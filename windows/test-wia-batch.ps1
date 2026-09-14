# Pure image/callback tests. No DeviceManager, Prepare or real Download is called.
$ErrorActionPreference = 'Stop'
$testCode = @'
using System;
using System.IO;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using ScanStation.Wia2;
public static class WiaBatchTests {
    static int checks;
    static void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
    sealed class Fake : IPageTransfer {
        public Func<IWiaTransferCallback, int> Action; public int Calls;
        public int Download(IWiaTransferCallback callback) { Calls++; return Action(callback); }
    }
    static byte[] ImageBytes(bool jpeg, Color color) {
        using (var bitmap = new Bitmap(40,60)) using(var g = Graphics.FromImage(bitmap)) using(var memory = new MemoryStream()) {
            g.Clear(color); bitmap.Save(memory, jpeg ? ImageFormat.Jpeg : ImageFormat.Bmp); return memory.ToArray();
        }
    }
    static void Page(IWiaTransferCallback callback, byte[] image) {
        IStream stream;
        Check(callback.GetNextStream(0,"ignored/../../page", "ignored", out stream) == 0, "GetNextStream succeeds");
        stream.Write(image, image.Length / 2, IntPtr.Zero);
        var rest = new byte[image.Length - image.Length / 2]; Array.Copy(image,image.Length / 2,rest,0,rest.Length);
        stream.Write(rest,rest.Length,IntPtr.Zero);
        TransferParameters end = new TransferParameters { Message = 2 };
        Check(callback.TransferCallback(0,ref end) == 0, "end complete stream");
    }
    public static string Run(string root) {
        byte[] jpeg = ImageBytes(true,Color.Red), bmp = ImageBytes(false,Color.Blue);
        var fake = new Fake { Action = cb => { for(int n=0;n<24;n++) Page(cb,n%2==0?jpeg:bmp); return 0; } };
        var result = BatchCapture.Run(fake,root,"batch",0);
        Check(fake.Calls == 1, "one Download for 24 pages");
        Check(result.Terminal == "ok:24" && result.Pages == 24 && result.Streams == 24,"24 pages counted");
        for(int n=1;n<=24;n++) {
            var path=Path.Combine(root,"batch-p"+n+".jpg");
            Check(File.Exists(path),"source page present");
            using(var image=new Bitmap(path)) Check(n%2==1?image.GetPixel(1,1).R>240:image.GetPixel(1,1).B>240,"source order");
        }
        Check(Convert.ToBase64String(File.ReadAllBytes(Path.Combine(root,"batch-p1.jpg"))) == Convert.ToBase64String(jpeg),"native JPEG bytes unchanged");
        Check(!File.Exists(Path.Combine(root,"batch-p2.capture")),"successful BMP spool is cleaned after durable JPEG");
        fake = new Fake { Action = cb => { Page(cb,jpeg); return 0; } };
        result = BatchCapture.Run(fake,root,"one",1);
        Check(result.Terminal == "ok:1" && fake.Calls==1,"single page success");
        fake = new Fake { Action = cb => { Page(cb,jpeg); IStream ignored; return cb.GetNextStream(0,"second","second",out ignored); } };
        result = BatchCapture.Run(fake,root,"overlimit",1);
        Check(result.Terminal.StartsWith("error pages=1:") && result.Streams==1,"second stream rejected before allocation");
        Check(!File.Exists(Path.Combine(root,"overlimit-p2.capture.part")),"no second page stream");
        fake = new Fake { Action = cb => { Page(cb,bmp); return unchecked((int)0x80210002); } };
        result = BatchCapture.Run(fake,root,"jam",0);
        Check(result.Terminal.StartsWith("error pages=1:") && result.Terminal.Contains("80210002"),"jam preserves completed page");
        fake = new Fake { Action = cb => { Page(cb,jpeg); return unchecked((int)0x80210003); } };
        Check(BatchCapture.Run(fake,root,"empty_after",0).Terminal == "ok:1","paper empty after completed batch normal");
        fake = new Fake { Action = cb => unchecked((int)0x80210003) };
        Check(BatchCapture.Run(fake,root,"empty_before",0).Terminal.StartsWith("error pages=0:"),"empty before first page error");
        fake = new Fake { Action = cb => { Page(cb,jpeg); throw new COMException("transport lost",unchecked((int)0x8021000A)); } };
        result=BatchCapture.Run(fake,root,"throw_after",0);
        Check(result.DownloadAttempted && fake.Calls==1 && result.Terminal.StartsWith("error pages=1:"),"exception never repeats download and retains page");
        fake = new Fake { Action = cb => { throw new COMException("failure before callback",unchecked((int)0x80004005)); } };
        result=BatchCapture.Run(fake,root,"throw_before",0);
        Check(result.DownloadAttempted && fake.Calls==1 && result.Terminal.StartsWith("error pages=0:"),"pre-callback Download failure still attempted");
        fake = new Fake { Action = cb => { IStream stream; cb.GetNextStream(0,"x","x",out stream); stream.Write(jpeg,10,IntPtr.Zero); return 0; } };
        result=BatchCapture.Run(fake,root,"partial",0);
        Check(result.Terminal.StartsWith("error pages=0:") && !File.Exists(Path.Combine(root,"partial-p1.jpg")),"partial not published");
        Check(File.Exists(Path.Combine(root,"partial-p1.capture.part")),"partial source retained");
        File.WriteAllText(Path.Combine(root,"blocked-p1.jpg"),"existing original");
        fake = new Fake { Action = cb => { Page(cb,jpeg); return 0; } };
        result=BatchCapture.Run(fake,root,"blocked",0);
        Check(result.Terminal.StartsWith("error pages=0:") && File.ReadAllText(Path.Combine(root,"blocked-p1.jpg"))=="existing original","publication collision never overwrites original or increments count");
        Check(File.Exists(Path.Combine(root,"blocked-p1.capture")),"failed publication retains complete capture stream");
        fake = new Fake { Action = cb => { Page(cb,jpeg); var error=new TransferParameters {Message=5,ErrorStatus=unchecked((int)0x80210014)}; cb.TransferCallback(0,ref error); return 0; } };
        result=BatchCapture.Run(fake,root,"multifeed",0);
        Check(result.Terminal.StartsWith("error pages=1:") && result.Terminal.Contains("80210014"),"device status fault overrides successful Download");
        foreach(int emptyResult in new int[] {0,1}) {
            fake = new Fake { Action = cb => emptyResult };
            Check(BatchCapture.Run(fake,root,"nopages"+emptyResult,0).Terminal.StartsWith("error pages=0:"),"empty successful transport is not successful capture");
        }
        fake = new Fake { Action = cb => { Page(cb,jpeg); return 0; } };
        Check(BatchCapture.Run(fake,root,"heartbeat_unreadable",0,Path.Combine(root,"missing-heartbeat.json")).Terminal=="ok:1","advisory heartbeat I/O cannot cancel a batch");
        fake = new Fake { Action = cb => { IStream stream; cb.GetNextStream(0,"x","x",out stream); stream.Write(jpeg,jpeg.Length,IntPtr.Zero); var end=new TransferParameters {Message=3}; cb.TransferCallback(0,ref end); return 0; } };
        Check(BatchCapture.Run(fake,root,"last_eot",1).Terminal=="ok:1","final successful EOT stream is validated and published without EOS");
        fake = new Fake { Action = cb => { IStream stream; cb.GetNextStream(0,"x","x",out stream); stream.Write(jpeg,jpeg.Length,IntPtr.Zero); var end=new TransferParameters {Message=3}; cb.TransferCallback(0,ref end); return unchecked((int)0x80210002); } };
        Check(BatchCapture.Run(fake,root,"failed_eot",1).Terminal.StartsWith("error pages=0:"),"failed Download never publishes unconfirmed EOT stream");
        fake = new Fake { Action = cb => { Page(cb,jpeg); return 1; } };
        Check(BatchCapture.Run(fake,root,"false_after",0).Terminal.StartsWith("error pages=1:"),"S_FALSE is not ok even after completed pages");
        fake = new Fake { Action = cb => { Page(cb,jpeg); var error=new TransferParameters {Message=5,ErrorStatus=unchecked((int)0x80210003)}; cb.TransferCallback(0,ref error); return unchecked((int)0x80210002); } };
        Check(BatchCapture.Run(fake,root,"empty_then_jam",0).Terminal.Contains("80210002"),"paper empty callback never masks final paper jam");
        fake = new Fake { Action = cb => { Page(cb,jpeg); var error=new TransferParameters {Message=5,ErrorStatus=unchecked((int)0x80210014)}; cb.TransferCallback(0,ref error); error.ErrorStatus=unchecked((int)0x80210003); cb.TransferCallback(0,ref error); return 0; } };
        Check(BatchCapture.Run(fake,root,"fault_then_empty",0).Terminal.Contains("80210014"),"late paper empty never masks earlier multifeed");
        fake = new Fake { Action = cb => { Page(cb,jpeg); IStream stream; cb.GetNextStream(0,"x","x",out stream); return unchecked((int)0x80210003); } };
        Check(BatchCapture.Run(fake,root,"empty_next",0).Terminal=="ok:1","empty next stream does not create phantom missing page");
        fake = new Fake { Action = cb => { IStream stream; cb.GetNextStream(0,"x","x",out stream); stream.Write(bmp,bmp.Length-20,IntPtr.Zero); var end=new TransferParameters {Message=3}; cb.TransferCallback(0,ref end); return 0; } };
        result=BatchCapture.Run(fake,root,"truncated_bmp",1);
        Check(result.Terminal.StartsWith("error pages=0:") && !File.Exists(Path.Combine(root,"truncated_bmp-p1.jpg")),"truncated BMP cannot be padded into a successful JPEG");
        using(var callback=new PageCallback(root,"cominterface",1)) {
            IntPtr callbackPointer=Marshal.GetComInterfaceForObject(callback,typeof(IWiaTransferCallback));
            Check(callbackPointer!=IntPtr.Zero,"native callback interface is exposed"); Marshal.Release(callbackPointer);
            IStream stream; callback.GetNextStream(0,"x","x",out stream);
            IntPtr streamPointer=Marshal.GetComInterfaceForObject(stream,typeof(IStream));
            Check(streamPointer!=IntPtr.Zero,"native IStream interface is exposed"); Marshal.Release(streamPointer);
        }
        Check(Marshal.SizeOf(typeof(TransferParameters))==24,"SDK transfer structure layout");
        return checks+" WIA2 simulated callback assertions passed; no hardware accessed.";
    }
}
'@
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('scan-station-wia2-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $testsPath = Join-Path $temporary 'tests.cs'
    [IO.File]::WriteAllText($testsPath, $testCode)
    Add-Type -Path @((Join-Path $PSScriptRoot 'wia-batch.cs'), $testsPath) -ReferencedAssemblies System.Drawing
    [WiaBatchTests]::Run($temporary)
}
finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
