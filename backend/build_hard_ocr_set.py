"""
Hard OCR stress test — Phase 0 Test 3.

Takes real pages from the Aurevia synthetic dataset and degrades them the
way a real broker's scan actually looks: blurred, rotated, low contrast,
JPEG-compressed, shadowed, noisy — then runs the same pipeline
(quality gate -> OCR -> extraction) against each one and reports what
actually happens.

The dataset's existing "_SCANNED" variants are computer-rendered text
turned into a clean image — genuinely useful for testing that OCR TRIGGERS
correctly, but not for testing OCR ACCURACY under real-world conditions.
This script builds the harder cases that were missing.

Each degradation is applied on its own AND in combination, so the report
can show where accuracy actually starts to break down rather than only a
single pass/fail on "hard OCR" as one blob.
"""
import io
import random
import sys
from pathlib import Path

import numpy as np
from pdf2image import convert_from_path
from PIL import Image, ImageEnhance, ImageFilter

# (this script already lives in backend/, no path insert needed)

OUTPUT_DIR = Path(__file__).parent / "hard_ocr_test"
OUTPUT_DIR.mkdir(exist_ok=True)

random.seed(42)  # reproducible degradation, so a re-run is comparable


def add_gaussian_noise(img: Image.Image, sigma: float) -> Image.Image:
    """Sensor noise, as from a phone camera in poor light."""
    arr = np.array(img).astype(np.float32)
    noise = np.random.normal(0, sigma, arr.shape)
    noisy = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(noisy)


def add_shadow(img: Image.Image, strength: float) -> Image.Image:
    """A diagonal darkening band, as from an overhead light or a hand
    holding a phone casting a shadow across part of the page."""
    arr = np.array(img).astype(np.float32)
    h, w = arr.shape[:2]
    gradient = np.tile(np.linspace(1.0, 1.0 - strength, w), (h, 1))
    if arr.ndim == 3:
        gradient = gradient[:, :, None]
    darkened = np.clip(arr * gradient, 0, 255).astype(np.uint8)
    return Image.fromarray(darkened)


def perspective_skew(img: Image.Image, max_shift_pct: float) -> Image.Image:
    """
    Approximates a phone-photo perspective distortion by shearing and
    slightly rotating — a true projective transform needs OpenCV, which is
    not available here, but this produces the same class of problem for
    OCR: text that is no longer perfectly horizontal or evenly spaced.
    """
    w, h = img.size
    shift = int(w * max_shift_pct)
    coeffs = (1, random.uniform(-0.08, 0.08), random.randint(-shift, shift),
              random.uniform(-0.02, 0.02), 1, 0)
    return img.transform(img.size, Image.AFFINE, coeffs, fillcolor="white")


DEGRADATIONS = {
    "01_clean_baseline": lambda img: img,
    "02_rotated_3deg": lambda img: img.rotate(3, expand=True, fillcolor="white"),
    "03_rotated_8deg": lambda img: img.rotate(8, expand=True, fillcolor="white"),
    "04_blur_moderate": lambda img: img.filter(ImageFilter.GaussianBlur(1.5)),
    "05_blur_heavy": lambda img: img.filter(ImageFilter.GaussianBlur(3.5)),
    "06_low_contrast": lambda img: ImageEnhance.Contrast(img).enhance(0.35),
    "07_low_resolution": lambda img: img.resize(
        (img.width // 3, img.height // 3)
    ).resize((img.width, img.height)),
    "08_shadow_band": lambda img: add_shadow(img, 0.55),
    "09_sensor_noise": lambda img: add_gaussian_noise(img, 25),
    "10_perspective_skew": lambda img: perspective_skew(img, 0.04),
    "11_multi_distortion_phone_photo": lambda img: add_gaussian_noise(
        add_shadow(
            perspective_skew(
                img.filter(ImageFilter.GaussianBlur(1.2)), 0.03
            ),
            0.35,
        ),
        15,
    ),
}


def jpeg_recompress(img: Image.Image, quality: int) -> Image.Image:
    """Repeated low-quality JPEG save/reload, as from a photo forwarded
    through WhatsApp several times — a real, common source of extra noise
    a broker's actual uploads go through before ever reaching this tool."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def build_degraded_set(source_pdf: Path) -> list[tuple[str, Path]]:
    """Render the source PDF's first page once, apply every degradation to
    that same source image, and save each as its own PDF so the existing
    pipeline (parse_file_with_quality) can be run against it unmodified."""
    pages = convert_from_path(str(source_pdf), dpi=200)
    base_image = pages[0].convert("RGB")

    results = []
    for name, transform in DEGRADATIONS.items():
        degraded = transform(base_image.copy())
        # Every case except the clean baseline also gets a JPEG
        # recompression pass, since a real scan is essentially never a
        # pristine lossless image by the time it reaches an inbox.
        if name != "01_clean_baseline":
            degraded = jpeg_recompress(degraded, quality=random.randint(35, 60))

        out_path = OUTPUT_DIR / f"{name}.pdf"
        degraded.save(out_path, "PDF", resolution=200.0)
        results.append((name, out_path))

    return results


if __name__ == "__main__":
    # Defaults to the exact source page used for the run recorded in
    # Results.md. Pass a different dataset root as argv[1] to build the same
    # 11 degradations from a different source document.
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
    else:
        source = Path(
            "/path/to/Aurevia_Synthetic_Insurance_World_v1/"
            "companies/AUR-SYN-003/01_property_policy_SCANNED.pdf"
        )
        print(
            "No source given — pass the path to a source PDF as argv[1]. "
            f"Trying the default (probably missing): {source}\n"
        )
    print(f"Building degraded variants from {source.name}...")
    variants = build_degraded_set(source)
    for name, path in variants:
        print(f"  {name} -> {path}")
    print(f"\n{len(variants)} variants written to {OUTPUT_DIR}")
