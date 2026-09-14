# 图像增强

`enhance_jpeg(data, mode)` 接收已经裁切、纠偏的 JPEG，只返回 JPEG 字节。它不识别文字、不改变像素尺寸，也不写入源文件。

| 模式 | 行为 |
| --- | --- |
| `original` | 原样返回裁切基图，不额外调色 |
| `enhanced` | 校正局部纸色和亮度，保留彩色字迹与印章 |
| `bw` | 在纸面校正后生成黑白页面 |

衍生图保留已有 DPI、EXIF 方向和图像尺寸。黑白结果以灰度 JPEG 保存；JPEG 压缩会在边缘产生少量近黑或近白的灰度值。PDF 直接嵌入对应的 RGB 或灰度 JPEG。原件、裁切基图和页面版本指针保持不变。

## 算法

实现使用 OpenCV、NumPy 和 Pillow，独立编写，没有打包第三方项目源码。

1. 将背景估计图缩至长边不超过 1200 像素，在各颜色通道做闭运算与高斯平滑，再放大背景图。按 `255 × pixel / background` 校正原尺寸页面。形态学操作只用于估计背景，不直接擦除正文。
2. 背景估计设下限，避免宽实心字、徽标和印章把自身归一化为白色。随后使用温和的单调明暗曲线，压深中间调并提亮接近白色的纸面。
3. 黑白分支混合亮度与最暗颜色通道，经双边滤波后结合 Sauvola 和局部均值阈值保留笔画。运算分块并保留邻域重叠，减少整页浮点数组与块边界接缝。

切换模式只保存文档偏好；渲染在文档锁外执行，并限制同时增强的数量。缓存按 `ENHANCEMENT_VERSION`、页面版本、源路径和模式区分。缓存丢失或损坏时从裁切基图重建；修改算法时应递增版本。

## 来源

- 低分辨率背景图与背景归一化思路参考 [Leptonica adaptmap.c](https://github.com/DanBloomberg/leptonica/blob/master/src/adaptmap.c)，项目使用 [BSD 两条款许可证](https://github.com/DanBloomberg/leptonica/blob/master/leptonica-license.txt)。
- 闭运算和背景平滑使用 [OpenCV 形态学操作](https://docs.opencv.org/4.13.0/d9/d61/tutorial_py_morphological_ops.html)；保边滤波使用 [OpenCV 双边滤波](https://docs.opencv.org/4.13.0/d4/d13/tutorial_py_filtering.html)。OpenCV 的授权说明见[官方许可证页面](https://opencv.org/license/)。
- Sauvola 公式参考 [scikit-image 文档](https://scikit-image.org/docs/stable/api/skimage.filters.html#skimage.filters.threshold_sauvola) 及原论文 [Adaptive document image binarization](https://doi.org/10.1016/S0031-3203(99)00055-2)。scikit-image 不是运行依赖。

## 验证范围

仓库内的合成测试覆盖明暗渐变纸面、深浅笔画、红蓝墨迹、实心图案、噪声、细线、小数点、尺寸与元数据、异常 JPEG，以及缓存和并发边界。没有附带真实扫描件或其测量记录。

增强不能可靠区分极淡文字与纸面污迹。黑白模式会改变灰阶和彩色内容的外观；核对颜色或极淡笔迹时，应切回原图或查看 raw 原件。
