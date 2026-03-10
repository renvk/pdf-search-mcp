"""CoreGraphics PDF renderer for macOS.

Uses Apple's native CoreGraphics/CoreText pipeline for sub-pixel font
rendering. Produces sharper glyphs than PyMuPDF's FreeType rasterizer,
especially for math fonts (CambriaMath, Computer Modern, STIX).

Requires pyobjc-framework-Quartz (auto-installed on macOS via pyproject.toml
platform marker). Import will fail on non-macOS — caller must catch ImportError.
"""

import Quartz
from CoreFoundation import CFURLCreateWithFileSystemPath, kCFAllocatorDefault
from Foundation import NSMutableData


def render_page_coregraphics(pdf_path, page_num, dpi=150):
    """Render a single PDF page to PNG bytes using CoreGraphics.

    Args:
        pdf_path: Absolute path to the PDF file (str).
        page_num: 1-based page number. CoreGraphics pages are 1-based natively.
        dpi: Resolution in dots per inch (default 150).

    Returns:
        PNG image as bytes. No file I/O — caller writes to disk if needed.

    Raises:
        ValueError: If the PDF cannot be opened (bad path) or page_num is
            out of range.
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

    # Media box defines the full page dimensions in points (1 pt = 1/72 in)
    media_box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    scale = dpi / 72.0
    width = int(media_box.size.width * scale)
    height = int(media_box.size.height * scale)

    # RGBA bitmap context — white background
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, cs,
        Quartz.kCGImageAlphaPremultipliedLast,
    )

    # White fill (PDF pages have transparent background by default)
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, width, height))

    # Enable font smoothing for Preview-quality text rendering
    Quartz.CGContextSetAllowsFontSmoothing(ctx, True)
    Quartz.CGContextSetShouldSmoothFonts(ctx, True)

    # Scale and apply the page's drawing transform (handles /Rotate, CropBox)
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    transform = Quartz.CGPDFPageGetDrawingTransform(
        page, Quartz.kCGPDFMediaBox, media_box, 0, True
    )
    Quartz.CGContextConcatCTM(ctx, transform)
    Quartz.CGContextDrawPDFPage(ctx, page)

    # Extract CGImage from the bitmap context
    cg_image = Quartz.CGBitmapContextCreateImage(ctx)

    # Encode to PNG in memory via CGImageDestination + NSMutableData
    png_data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(
        png_data, "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, cg_image, None)
    Quartz.CGImageDestinationFinalize(dest)

    return bytes(png_data)
