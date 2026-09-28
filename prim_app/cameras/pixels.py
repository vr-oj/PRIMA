"""Owned camera pixels and display conversion; TIFF data is never display-scaled."""
import numpy as np
from PyQt5.QtGui import QImage


def display_gray(pixels, bit_depth=None):
    pixels = np.asarray(pixels)
    if pixels.ndim == 2 and pixels.dtype in (np.dtype("uint8"), np.dtype("uint16")):
        depth = bit_depth or pixels.dtype.itemsize * 8
        if not 1 <= depth <= pixels.dtype.itemsize * 8:
            raise ValueError("Invalid camera bit depth")
        return np.ascontiguousarray(np.clip(pixels >> max(0, depth - 8), 0, 255), dtype=np.uint8)
    if pixels.ndim == 3 and pixels.shape[2] in (3, 4) and pixels.dtype == np.uint8:
        rgb = pixels[:, :, :3].astype(np.uint16)
        return np.ascontiguousarray((rgb[:, :, 0] * 77 + rgb[:, :, 1] * 150 + rgb[:, :, 2] * 29) >> 8, dtype=np.uint8)
    raise ValueError(f"Unsupported image layout: {pixels.dtype}, {pixels.shape}")


def copy_mm_frame(data, components, bit_depth):
    data = np.asarray(data)
    if components == 1 and data.ndim == 2 and data.dtype in (np.dtype("uint8"), np.dtype("uint16")):
        pixels = np.array(data, order="C", copy=True)
        preview = display_gray(pixels, bit_depth)
        fmt, pixel_format = QImage.Format_Grayscale8, f"Mono{bit_depth}"
    elif components == 4 and bit_depth == 8 and data.dtype == np.uint32 and data.ndim == 2:
        pixels = np.stack(((data >> 16) & 255, (data >> 8) & 255, data & 255), axis=-1).astype(np.uint8)
        preview, fmt, pixel_format = pixels, QImage.Format_RGB888, "RGB8"
    else:
        raise ValueError(f"Unsupported Micro-Manager image layout: {data.dtype}, {data.shape}, {components} components")
    height, width = preview.shape[:2]
    image = QImage(preview.data, width, height, preview.strides[0], fmt).copy()
    return image, pixels, pixel_format
