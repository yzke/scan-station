// Native WIA 2 definitions follow Microsoft's SDK wia_lh.h/WiaDef.h.
// Reference: Windows-classic-samples, multimedia/wia/datatransfer/DataTransfer.cpp.
// Loading this assembly does not instantiate COM or acquire any image.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text.RegularExpressions;
using System.Threading;
using STATSTG = System.Runtime.InteropServices.ComTypes.STATSTG;

namespace ScanStation.Wia2
{
    [StructLayout(LayoutKind.Sequential)]
    public struct TransferParameters
    {
        public int Message, PercentComplete;
        public ulong TransferredBytes;
        public int ErrorStatus;
    }

    [ComVisible(true), Guid("27D4EAAF-28A6-4CA5-9AAB-E678168B9527"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IWiaTransferCallback
    {
        [PreserveSig] int TransferCallback(int flags, ref TransferParameters parameters);
        [PreserveSig] int GetNextStream(int flags, [MarshalAs(UnmanagedType.BStr)] string itemName,
            [MarshalAs(UnmanagedType.BStr)] string fullName, [MarshalAs(UnmanagedType.Interface)] out IStream stream);
    }

    // Only the used leading vtable slots are declared. No omitted slot precedes a used method.
    [ComImport, Guid("79C07CF1-CBDD-41EE-8EC3-F00080CADA7A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IWiaDevMgr2
    {
        [PreserveSig] int EnumDeviceInfo(int flags, out IntPtr items);
        [PreserveSig] int CreateDevice(int flags, [MarshalAs(UnmanagedType.BStr)] string id, out IWiaItem2 root);
    }
    [ComImport, Guid("6CBA0075-1287-407D-9B77-CF0E030435CC"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IWiaItem2
    {
        [PreserveSig] int CreateChildItem(int flags, int creationFlags, [MarshalAs(UnmanagedType.BStr)] string name, out IWiaItem2 item);
        [PreserveSig] int DeleteItem(int flags);
        [PreserveSig] int EnumChildItems(IntPtr category, out IEnumWiaItem2 items);
        [PreserveSig] int FindItemByName(int flags, [MarshalAs(UnmanagedType.BStr)] string name, out IWiaItem2 item);
        [PreserveSig] int GetItemCategory(out Guid category);
        [PreserveSig] int GetItemType(out int itemType);
    }
    [ComImport, Guid("59970AF4-CD0D-44D9-AB24-52295630E582"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IEnumWiaItem2
    {
        [PreserveSig] int Next(uint count, out IWiaItem2 item, out uint fetched);
    }
    [ComImport, Guid("C39D6942-2F4E-4D04-92FE-4EF4D3A1DE5A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IWiaTransfer
    {
        [PreserveSig] int Download(int flags, IWiaTransferCallback callback);
        [PreserveSig] int Upload(int flags, IStream source, IWiaTransferCallback callback);
        [PreserveSig] int Cancel();
        [PreserveSig] int EnumWIA_FORMAT_INFO(out IEnumWiaFormat formats);
    }
    [StructLayout(LayoutKind.Sequential)]
    struct FormatInfo { public Guid Format; public int Tymed; }
    [ComImport, Guid("81BEFC5B-656D-44F1-B24C-D41D51B4DC81"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IEnumWiaFormat
    {
        [PreserveSig] int Next(uint count, out FormatInfo format, out uint fetched);
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PropertySpec { public uint Kind; public IntPtr Id; }
    [ComImport, Guid("98B5E8A0-29CC-491A-AAC0-E6DB4FDCCEB6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IWiaPropertyStorage
    {
        [PreserveSig] int ReadMultiple(uint count, ref PropertySpec spec, IntPtr value);
        [PreserveSig] int WriteMultiple(uint count, ref PropertySpec spec, IntPtr value, uint firstName);
    }

    public sealed class BatchUnavailableException : Exception
    {
        public bool ConfigurationChanged { get; private set; }
        public BatchUnavailableException(string message, Exception inner, bool changed) : base(message, inner) { ConfigurationChanged = changed; }
    }
    public interface IPageTransfer { int Download(IWiaTransferCallback callback); }

    public sealed class NativeSession : IPageTransfer, IDisposable
    {
        static readonly Guid Feeder = new Guid("FE131934-F84C-42AD-8DA4-6129CDDD7288");
        static readonly Guid Jpeg = new Guid("B96B3CAE-0728-11D3-9D7B-0000F81EF32E");
        static readonly Guid Bmp = new Guid("B96B3CAB-0728-11D3-9D7B-0000F81EF32E");
        object manager;
        IWiaItem2 root, feeder;
        IWiaTransfer transfer;
        bool attempted, configurationChanged;
        public string Format { get; private set; }
        public int MaxPages { get; private set; }
        public bool DownloadAttempted { get { return attempted; } }

        // This method only connects/sets properties. It cannot start acquisition.
        public static NativeSession Prepare(string deviceId, int dpi, bool duplex, int maxPages)
        {
            if (String.IsNullOrWhiteSpace(deviceId)) throw new ArgumentException("Missing WIA device ID.");
            if (dpi != 150 && dpi != 200 && dpi != 300) throw new ArgumentException("Unsupported DPI.");
            if (maxPages != 0 && maxPages != 1) throw new ArgumentException("Only all pages or one page is supported.");
            NativeSession session = new NativeSession();
            try
            {
                session.manager = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("B6C292BC-7C88-41EE-8B54-8EC92617E599")));
                Check(((IWiaDevMgr2)session.manager).CreateDevice(0, deviceId, out session.root));
                IEnumWiaItem2 items;
                Check(session.root.EnumChildItems(IntPtr.Zero, out items));
                try
                {
                    while (true)
                    {
                        IWiaItem2 item; uint fetched;
                        int hr = items.Next(1, out item, out fetched);
                        Check(hr);
                        if (fetched == 0) break;
                        Guid category;
                        try
                        {
                            Check(item.GetItemCategory(out category));
                            if (category == Feeder) { session.feeder = item; item = null; break; }
                        }
                        finally { Release(item); }
                    }
                }
                finally { Release(items); }
                if (session.feeder == null) throw new NotSupportedException("No WIA 2 feeder item.");
                session.transfer = (IWiaTransfer)session.feeder;
                IWiaPropertyStorage properties = (IWiaPropertyStorage)session.feeder;
                Guid format = session.ChooseFormat();
                session.configurationChanged = true;
                WriteGuid(properties, 4106, format);
                // FRONT_ONLY=32 and DUPLEX=4 are the WIA 2 modes; select the feeder item itself.
                WriteInt(properties, 3088, maxPages == 1 || !duplex ? 32 : 4);
                WriteInt(properties, 6147, dpi);
                WriteInt(properties, 6148, dpi);
                WriteInt(properties, 6149, 0);
                WriteInt(properties, 6150, 0);
                WriteInt(properties, 6151, (int)Math.Round(210.0 / 25.4 * dpi));
                WriteInt(properties, 6152, (int)Math.Round(297.0 / 25.4 * dpi));
                WriteInt(properties, 3096, maxPages); // ALL_PAGES=0; strict single-side rescan=1.
                session.Format = format == Jpeg ? "jpeg" : "bmp";
                session.MaxPages = maxPages;
                return session;
            }
            catch (Exception error)
            {
                session.Dispose();
                // No Download was called, so caller may explicitly choose legacy compatibility.
                throw new BatchUnavailableException("The driver cannot initialize continuous feeder capture: " + error.Message, error, session.configurationChanged);
            }
        }

        Guid ChooseFormat()
        {
            IEnumWiaFormat formats;
            Check(transfer.EnumWIA_FORMAT_INFO(out formats));
            bool bmp = false, jpeg = false;
            try
            {
                while (true)
                {
                    FormatInfo info; uint fetched;
                    Check(formats.Next(1, out info, out fetched));
                    if (fetched == 0) break;
                    if (info.Format == Jpeg) jpeg = true;
                    if (info.Format == Bmp) bmp = true;
                }
            }
            finally { Release(formats); }
            if (jpeg) return Jpeg;
            if (bmp) return Bmp;
            throw new NotSupportedException("Driver exposes neither per-page JPEG nor BMP streams.");
        }

        public int Download(IWiaTransferCallback callback)
        {
            if (attempted) throw new InvalidOperationException("A capture session cannot be downloaded twice.");
            attempted = true; // Set BEFORE COM call; even an immediate exception must never be retried.
            return transfer.Download(0, callback);
        }
        static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }
        static PropertySpec Spec(int id) { return new PropertySpec { Kind = 1, Id = new IntPtr(id) }; }
        static IntPtr Variant()
        {
            int size = IntPtr.Size == 8 ? 24 : 16;
            IntPtr value = Marshal.AllocCoTaskMem(size);
            Marshal.Copy(new byte[size], 0, value, size);
            return value;
        }
        [DllImport("ole32.dll")] static extern int PropVariantClear(IntPtr value);
        static void FreeVariant(IntPtr value) { PropVariantClear(value); Marshal.FreeCoTaskMem(value); }
        static void WriteInt(IWiaPropertyStorage properties, int id, int value)
        {
            PropertySpec spec = Spec(id);
            IntPtr variant = Variant();
            try
            {
                Marshal.WriteInt16(variant, 0, 3); // VT_I4
                Marshal.WriteInt32(variant, 8, value);
                Check(properties.WriteMultiple(1, ref spec, variant, 2));
            }
            finally { FreeVariant(variant); }
            variant = Variant();
            try
            {
                Check(properties.ReadMultiple(1, ref spec, variant));
                int kind = Marshal.ReadInt16(variant);
                if ((kind != 3 && kind != 19) || Marshal.ReadInt32(variant, 8) != value)
                    throw new InvalidOperationException("Scanner rejected property " + id + "=" + value + ".");
            }
            finally { FreeVariant(variant); }
        }
        static void WriteGuid(IWiaPropertyStorage properties, int id, Guid value)
        {
            PropertySpec spec = Spec(id);
            IntPtr variant = Variant();
            try
            {
                Marshal.WriteInt16(variant, 0, 72); // VT_CLSID
                IntPtr guid = Marshal.AllocCoTaskMem(16);
                Marshal.StructureToPtr(value, guid, false);
                Marshal.WriteIntPtr(variant, 8, guid);
                Check(properties.WriteMultiple(1, ref spec, variant, 2));
            }
            finally { FreeVariant(variant); }
            variant = Variant();
            try
            {
                Check(properties.ReadMultiple(1, ref spec, variant));
                if (Marshal.ReadInt16(variant) != 72 || (Guid)Marshal.PtrToStructure(Marshal.ReadIntPtr(variant, 8), typeof(Guid)) != value)
                    throw new InvalidOperationException("Scanner rejected image format.");
            }
            finally { FreeVariant(variant); }
        }
        static void Release(object value)
        {
            if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
        }
        public void Dispose()
        {
            transfer = null; // Same RCW as feeder; release only once.
            Release(feeder); feeder = null;
            Release(root); root = null;
            Release(manager); manager = null;
        }
    }

    public sealed class CaptureResult
    {
        public int Pages { get; internal set; }
        public int Streams { get; internal set; }
        public string Terminal { get; internal set; }
        public bool DownloadAttempted { get; internal set; }
    }

    public static class BatchCapture
    {
        public static CaptureResult Run(IPageTransfer transfer, string outputDirectory, string id, int maxPages)
        { return Run(transfer, outputDirectory, id, maxPages, null); }
        public static CaptureResult Run(IPageTransfer transfer, string outputDirectory, string id, int maxPages, string heartbeatPath)
        {
            using (PageCallback callback = new PageCallback(outputDirectory, id, maxPages, heartbeatPath))
            {
                int hr;
                try { hr = transfer.Download(callback); }
                catch (Exception error) { hr = Marshal.GetHRForException(error); callback.RecordFailure("Download failed: " + error.Message); }
                return callback.Complete(hr);
            }
        }
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class PageCallback : IWiaTransferCallback, IDisposable
    {
        const int Abort = unchecked((int)0x80004004);
        const int PaperEmpty = unchecked((int)0x80210003);
        readonly string output, id;
        readonly int maximum;
        readonly string heartbeatPath;
        DateTime lastHeartbeat = DateTime.MinValue;
        // Eight durable file paths, never image buffers or COM objects. Backpressure
        // bounds queued work when JPEG encoding is slower than the feeder.
        readonly BlockingCollection<PageFile> queue = new BlockingCollection<PageFile>(8);
        readonly Thread publisher;
        readonly object sync = new object();
        PageStream current;
        string failure;
        int issued, completed, deviceError;
        bool finished, sawEndOfTransfer;
        sealed class PageFile { public string Path; public int Number; }
        public PageCallback(string outputDirectory, string batchId, int maxPages)
            : this(outputDirectory, batchId, maxPages, null) { }
        public PageCallback(string outputDirectory, string batchId, int maxPages, string heartbeatPath)
        {
            if (!Regex.IsMatch(batchId ?? "", "^[A-Za-z0-9_-]{1,120}$")) throw new ArgumentException("Invalid batch ID.");
            if (maxPages != 0 && maxPages != 1) throw new ArgumentException("Invalid page limit.");
            output = outputDirectory; id = batchId; maximum = maxPages; this.heartbeatPath = heartbeatPath;
            Directory.CreateDirectory(output);
            publisher = new Thread(PublishPages); publisher.IsBackground = true; publisher.Name = "ScanStation JPEG publisher";
            publisher.Start();
        }
        public void RecordFailure(string message) { lock (sync) { if (failure == null) failure = message; } }
        bool Failed { get { lock (sync) { return failure != null; } } }
        public int GetNextStream(int flags, string itemName, string fullName, out IStream stream)
        {
            stream = null;
            try
            {
                if (Failed || finished) return Abort;
                Heartbeat();
                if (current != null) throw new InvalidOperationException("Next page arrived before END_OF_STREAM.");
                if (maximum != 0 && issued >= maximum) throw new InvalidOperationException("Driver exceeded the one-page limit.");
                int next = issued + 1;
                current = new PageStream(Path.Combine(output, id + "-p" + next + ".capture.part"));
                issued = next;
                stream = current;
                return 0;
            }
            catch (Exception error) { RecordFailure(error.Message); return Abort; }
        }
        public int TransferCallback(int flags, ref TransferParameters parameters)
        {
            try
            {
                Heartbeat();
                if (parameters.Message == 5 && parameters.ErrorStatus < 0)
                {
                    if (deviceError == 0 || deviceError == PaperEmpty) deviceError = parameters.ErrorStatus;
                    return parameters.ErrorStatus;
                }
                if (parameters.Message == 3) sawEndOfTransfer = true;
                if (parameters.Message == 2)
                {
                    if (current == null) throw new InvalidOperationException("END_OF_STREAM has no open page.");
                    string raw = current.Finish();
                    current = null;
                    queue.Add(new PageFile { Path = raw, Number = issued });
                }
                return Failed ? Abort : 0;
            }
            catch (Exception error) { RecordFailure(error.Message); return Abort; }
        }
        void Heartbeat()
        {
            if (heartbeatPath == null || (DateTime.UtcNow - lastHeartbeat).TotalSeconds < 5) return;
            lastHeartbeat = DateTime.UtcNow;
            // Status is advisory. A sharing violation from a concurrent SMB read
            // must not cancel physical acquisition or discard an otherwise good page.
            try
            {
                string json = File.ReadAllText(heartbeatPath);
                json = Regex.Replace(json, "\"checked_at\"\\s*:\\s*\"[^\"]*\"", "\"checked_at\":\"" + DateTime.UtcNow.ToString("o") + "\"");
                string temporary = heartbeatPath + ".wia2.tmp";
                File.WriteAllText(temporary, json, new System.Text.UTF8Encoding(false));
                File.Replace(temporary, heartbeatPath, null);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        void PublishPages()
        {
            bool publicationFailed = false;
            foreach (PageFile page in queue.GetConsumingEnumerable())
            {
                if (publicationFailed) continue; // Keep source numbering contiguous; retain later raw files.
                try
                {
                    string temporary = Path.Combine(output, id + "-p" + page.Number + ".jpg.part");
                    string target = Path.Combine(output, id + "-p" + page.Number + ".jpg");
                    using (FileStream source = new FileStream(page.Path, FileMode.Open, FileAccess.Read, FileShare.Read))
                    {
                        ValidateBitmapLength(source);
                        using (Image image = Image.FromStream(source, false, true))
                        using (FileStream destination = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        {
                        if (image.RawFormat.Guid == ImageFormat.Jpeg.Guid)
                        {
                            source.Position = source.Length - 2;
                            if (source.ReadByte() != 255 || source.ReadByte() != 217) throw new InvalidDataException("JPEG stream is incomplete.");
                            source.Position = 0;
                            source.CopyTo(destination); // Native JPEG: no decode/re-encode round trip.
                        }
                        else if (image.RawFormat.Guid == ImageFormat.Bmp.Guid)
                            image.Save(destination, ImageFormat.Jpeg);
                        else throw new InvalidDataException("Unexpected or multipage image format.");
                        destination.Flush(true);
                        }
                    }
                    File.Move(temporary, target); // Never overwrite an existing completed page.
                    lock (sync) { completed++; }
                    // Successful JPEG is now the durable raw page used by Linux.
                    // Failed/unpublished streams remain available for recovery.
                    try { File.Delete(page.Path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                }
                catch (Exception error) { publicationFailed = true; RecordFailure("Page publication failed: " + error.Message); }
            }
        }
        static void ValidateBitmapLength(FileStream source)
        {
            // GDI+ can decode some truncated BMPs by filling missing pixels.
            // Check the complete file and row storage before any JPEG conversion.
            using (BinaryReader reader = new BinaryReader(source, System.Text.Encoding.UTF8, true))
            {
                if (source.Length < 2) throw new InvalidDataException("Empty image stream.");
                if (reader.ReadUInt16() != 0x4D42) { source.Position = 0; return; }
                if (source.Length < 54) throw new InvalidDataException("BMP header is incomplete.");
                uint declared = reader.ReadUInt32();
                source.Position = 10;
                uint offset = reader.ReadUInt32(), header = reader.ReadUInt32();
                int width = reader.ReadInt32(), height = reader.ReadInt32();
                ushort planes = reader.ReadUInt16(), bits = reader.ReadUInt16();
                uint compression = reader.ReadUInt32();
                if (declared != source.Length || header < 40 || offset < 14L + header || width <= 0 || height == 0 || planes != 1 ||
                    (bits != 1 && bits != 4 && bits != 8 && bits != 16 && bits != 24 && bits != 32) ||
                    (compression != 0 && compression != 3 && compression != 6))
                    throw new InvalidDataException("BMP storage header is invalid or incomplete.");
                long pixels = checked((((long)width * bits + 31) / 32) * 4 * Math.Abs((long)height));
                if (offset + pixels > source.Length) throw new InvalidDataException("BMP pixel data is incomplete.");
                source.Position = 0;
            }
        }
        public CaptureResult Complete(int downloadResult)
        {
            if (finished) throw new InvalidOperationException("Batch completion called twice.");
            finished = true;
            // S_FALSE is a cancelled/unsuccessful transfer, not a successful batch.
            // A paper-empty callback must never hide a different final HRESULT.
            if (downloadResult != 0 && downloadResult != PaperEmpty)
                RecordFailure("Image transfer failed (WIA 0x" + unchecked((uint)downloadResult).ToString("X8") + ").");
            if (deviceError != 0 && deviceError != PaperEmpty)
                RecordFailure("Device reported an error (WIA 0x" + unchecked((uint)deviceError).ToString("X8") + ").");
            if (current != null)
            {
                if (sawEndOfTransfer && downloadResult == 0 && deviceError == 0 && !Failed)
                {
                    // WIA sends EOS when the next stream is requested. The last
                    // stream may instead end at EOT; wait for successful Download
                    // before sealing it, then the publisher validates the image.
                    try { queue.Add(new PageFile { Path = current.Finish(), Number = issued }); }
                    catch (Exception error) { RecordFailure("Final page could not be completed: " + error.Message); }
                    finally { current.Dispose(); current = null; }
                }
                else if (maximum == 0 && !Failed && current.Length == 0 && issued > 1 &&
                    (downloadResult == PaperEmpty || deviceError == PaperEmpty))
                {
                    // Some feeders ask for an empty next stream before discovering
                    // empty paper. It has no image and must not create a phantom gap.
                    current.Dispose(); current = null; issued--;
                }
                else
                {
                    current.Dispose(); current = null;
                    RecordFailure("Download ended before the current page was complete.");
                }
            }
            queue.CompleteAdding();
            publisher.Join(); // Worker only uses managed files/images, never COM or the capture thread.
            if ((downloadResult == PaperEmpty || deviceError == PaperEmpty) && !(maximum == 0 && completed > 0))
                RecordFailure("Feeder is empty (WIA 0x80210003).");
            if (completed != issued && !Failed) RecordFailure("Not all received page streams were published.");
            if (completed == 0 && !Failed) RecordFailure("No complete page was received.");
            if (maximum == 1 && completed != 1 && !Failed) RecordFailure("One-page capture did not produce exactly one page.");
            return new CaptureResult { Pages = completed, Streams = issued, DownloadAttempted = true,
                Terminal = Failed ? "error pages=" + completed + ": " + failure : "ok:" + completed };
        }
        public void Dispose()
        {
            if (current != null) { current.Dispose(); current = null; }
            if (!queue.IsAddingCompleted) queue.CompleteAdding();
            publisher.Join();
            queue.Dispose();
        }
    }

    // Managed COM stream: WIA owns the interface; the callback owns file lifetime.
    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class PageStream : IStream, IDisposable
    {
        readonly string path;
        FileStream file;
        public PageStream(string path)
        {
            this.path = path;
            file = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read);
        }
        public string Finish()
        {
            file.Flush(true); file.Dispose(); file = null;
            string completed = path.Substring(0, path.Length - ".part".Length);
            File.Move(path, completed);
            return completed;
        }
        public long Length { get { return file.Length; } }
        public void Read(byte[] bytes, int count, IntPtr read) { int n = file.Read(bytes, 0, count); if (read != IntPtr.Zero) Marshal.WriteInt32(read, n); }
        public void Write(byte[] bytes, int count, IntPtr written) { file.Write(bytes, 0, count); if (written != IntPtr.Zero) Marshal.WriteInt32(written, count); }
        public void Seek(long offset, int origin, IntPtr position) { long p = file.Seek(offset, (SeekOrigin)origin); if (position != IntPtr.Zero) Marshal.WriteInt64(position, p); }
        public void SetSize(long size) { file.SetLength(size); }
        public void Commit(int flags) { file.Flush(true); }
        public void Stat(out STATSTG stat, int flags) { stat = new STATSTG { type = 2, cbSize = file.Length, grfMode = 2 }; if ((flags & 1) == 0) stat.pwcsName = path; }
        public void CopyTo(IStream target, long count, IntPtr read, IntPtr written)
        {
            byte[] buffer = new byte[65536]; long total = 0;
            while (total < count)
            {
                int n = file.Read(buffer, 0, (int)Math.Min(buffer.Length, count - total));
                if (n == 0) break;
                target.Write(buffer, n, IntPtr.Zero); total += n;
            }
            if (read != IntPtr.Zero) Marshal.WriteInt64(read, total);
            if (written != IntPtr.Zero) Marshal.WriteInt64(written, total);
        }
        public void Revert() { throw new COMException("Not a transacted stream.", unchecked((int)0x80030001)); }
        public void LockRegion(long offset, long count, int type) { throw new COMException("Region locks are not supported.", unchecked((int)0x80030001)); }
        public void UnlockRegion(long offset, long count, int type) { throw new COMException("Region locks are not supported.", unchecked((int)0x80030001)); }
        public void Clone(out IStream clone) { clone = null; throw new COMException("Stream cloning is not supported.", unchecked((int)0x80030001)); }
        public void Dispose() { if (file != null) { file.Dispose(); file = null; } }
    }
}
