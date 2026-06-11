"""CoreGraphics PDF renderer for macOS.

Uses Apple's native CoreGraphics/CoreText pipeline for sub-pixel font
rendering. Produces sharper glyphs than PyMuPDF's FreeType rasterizer,
especially for math fonts (CambriaMath, Computer Modern, STIX).

Geometry invariant: the bitmap is sized from the rotation-applied CropBox,
matching PyMuPDF's page.rect — callers compute clip rectangles from
page.rect and both renderers must agree on the coordinate space, or
region crops land on the wrong content.

Requires pyobjc-framework-Quartz (auto-installed on macOS via pyproject.toml
platform marker). Import will fail on non-macOS — caller must catch ImportError.
"""

import Quartz
from CoreFoundation import CFURLCreateWithFileSystemPath, kCFAllocatorDefault
from Foundation import NSMutableData


def render_page_coregraphics(pdf_path, page_num, dpi=140, clip_rect=None):
    """Render a single PDF page (or region) to PNG bytes using CoreGraphics.

    Args:
        pdf_path: Absolute path to the PDF file (str).
        page_num: 1-based page number. CoreGraphics pages are 1-based natively.
        dpi: Resolution in dots per inch (default 140, must be >= 1).
        clip_rect: Optional (x, y, w, h) tuple in PDF points, CG bottom-left
            origin, in the rotation-applied CropBox coordinate space (same
            space as PyMuPDF's page.rect). When set, bitmap is sized to this
            region and only content within it is rendered.

    Returns:
        PNG image as bytes. No file I/O — caller writes to disk if needed.

    Raises:
        ValueError: If the PDF cannot be opened, page_num is out of range,
            the bitmap context cannot be created (zero/negative size), or
            PNG encoding fails.
    """
    url = CFURLCreateWithFileSystemPath(
        kCFAllocatorDefault, pdf_path, Quartz.kCFURLPOSIXPathStyle, False
    )
    pdf_doc = Quartz.CGPDFDocumentCreateWithURL(url)
    if pdf_doc is None:
        raise ValueError(f"Cannot open PDF: {pdf_path}")

    page = Quartz.CGPDFDocumentGetPage(pdf_doc, page_num)
    if page is None:
        raise ValueError(
            f"Page {page_num} out of range for {pdf_path}"
        )

    # CropBox defines the visible content area (excludes bleed/trim marks);
    # CGPDFPageGetBoxRect falls back to MediaBox when no CropBox is defined.
    # Apply /Rotate so dimensions match PyMuPDF's page.rect, which callers
    # use to compute clip rectangles.
    crop_box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFCropBox)
    rotation = Quartz.CGPDFPageGetRotationAngle(page) % 360
    page_w, page_h = crop_box.size.width, crop_box.size.height
    if rotation in (90, 270):
        page_w, page_h = page_h, page_w

    scale = dpi / 72.0

    if clip_rect is not None:
        cx, cy, cw, ch = clip_rect
        width = int(cw * scale)
        height = int(ch * scale)
    else:
        width = int(page_w * scale)
        height = int(page_h * scale)
    if width < 1 or height < 1:
        raise ValueError(f"Render size {width}x{height} px is invalid (dpi={dpi}).")

    # RGBA bitmap context — white background
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, cs,
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    if ctx is None:
        raise ValueError(f"Cannot create {width}x{height} px bitmap context.")

    # White fill (PDF pages have transparent background by default)
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, width, height))

    # Enable font smoothing for Preview-quality text rendering
    Quartz.CGContextSetAllowsFontSmoothing(ctx, True)
    Quartz.CGContextSetShouldSmoothFonts(ctx, True)

    # Scale to target DPI, then shift so the clip region (if any) fills the bitmap
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    if clip_rect is not None:
        # Translate so the clip region's origin maps to bitmap (0, 0).
        # After ScaleCTM the context operates in point space, so shifting by
        # (-cx, -cy) moves the region's bottom-left corner to the origin.
        # Content outside the bitmap bounds is clipped implicitly by CG.
        Quartz.CGContextTranslateCTM(ctx, -cx, -cy)
    # Map the CropBox (with /Rotate applied) onto a rect of exactly its own
    # rotated size at the origin — content fills the bitmap edge to edge.
    # Sizing the target rect from any other box (e.g. MediaBox) letterboxes
    # the content and shifts clip coordinates off their targets.
    target = Quartz.CGRectMake(0, 0, page_w, page_h)
    transform = Quartz.CGPDFPageGetDrawingTransform(
        page, Quartz.kCGPDFCropBox, target, 0, True
    )
    Quartz.CGContextConcatCTM(ctx, transform)
    Quartz.CGContextDrawPDFPage(ctx, page)

    # Extract CGImage from the bitmap context
    cg_image = Quartz.CGBitmapContextCreateImage(ctx)
    if cg_image is None:
        raise ValueError("Failed to extract image from bitmap context.")

    # Encode to PNG in memory via CGImageDestination + NSMutableData
    png_data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(
        png_data, "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, cg_image, None)
    if not Quartz.CGImageDestinationFinalize(dest) or not len(png_data):
        raise ValueError("PNG encoding failed.")

    return bytes(png_data)
