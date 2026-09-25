import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from .config import Config
from .errors import ApiError
from .options import DEFAULT_QUALITY, FORMATS, ProcessOptions, validate_quality

AXIS_SWAPPING_ORIENTATIONS = {5, 6, 7, 8}
PNG_COMPRESSION_LEVEL = 6
ENCODER_THREADS = 1


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    content_type: str
    width: int
    height: int


def transform(source: bytes, options: ProcessOptions, config: Config) -> ImageResult:
    """Resize a static image, strip metadata, and encode the requested format."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source), formats=[name.upper() for name in FORMATS]) as image:
                _validate_image(image, config.max_input_pixels)
                image_format = options.format or (image.format or "").lower()
                validate_quality(image_format, options.quality)
                size = _output_size(image, options, config.max_output_pixels)
                with _resize_image(image, size, options.crop) as resized:
                    data = _encode_image(resized, image_format, options.quality)
        if len(data) > config.max_output_bytes:
            raise ApiError(413, "IMAGE_TOO_LARGE", "Result exceeds the output byte limit")
        return ImageResult(
            data=data, content_type=f"image/{image_format}", width=size[0], height=size[1]
        )
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Source exceeds the input pixel limit") from None
    except UnidentifiedImageError, OSError, ValueError, SyntaxError:
        raise ApiError(415, "INVALID_IMAGE", "Source is not a valid supported image") from None


def _validate_image(image: Image.Image, max_pixels: int) -> None:
    if image.width * image.height > max_pixels:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Source exceeds the input pixel limit")
    if getattr(image, "n_frames", 1) > 1:
        raise ApiError(415, "UNSUPPORTED_IMAGE", "Use a static JPEG, PNG, WebP, or AVIF image")


def _output_size(image: Image.Image, options: ProcessOptions, max_pixels: int) -> tuple[int, int]:
    input_width, input_height = image.size
    if image.getexif().get(ExifTags.Base.Orientation) in AXIS_SWAPPING_ORIENTATIONS:
        input_width, input_height = input_height, input_width

    if options.width and options.height:
        if options.crop == "fit":
            scale = min(options.width / input_width, options.height / input_height)
            width = max(1, int(input_width * scale + 0.5))
            height = max(1, int(input_height * scale + 0.5))
        else:
            width, height = options.width, options.height
    elif options.width:
        width = options.width
        height = max(1, int(input_height * width / input_width + 0.5))
    elif options.height:
        height = options.height
        width = max(1, int(input_width * height / input_height + 0.5))
    else:
        width, height = input_width, input_height

    if width * height > max_pixels:
        raise ApiError(
            413,
            "IMAGE_TOO_LARGE",
            "Result exceeds the output pixel limit; request smaller dimensions",
        )
    return width, height


def _resize_image(image: Image.Image, size: tuple[int, int], crop: str) -> Image.Image:
    with ImageOps.exif_transpose(image) as oriented:
        has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
        with oriented.convert("RGBA" if has_alpha else "RGB") as normalized:
            if crop == "fill":
                return ImageOps.fit(normalized, size, method=Image.Resampling.LANCZOS)
            if crop == "pad":
                return ImageOps.pad(
                    normalized, size, method=Image.Resampling.LANCZOS, color="white"
                )
            return normalized.resize(size, Image.Resampling.LANCZOS)


def _encode_image(image: Image.Image, image_format: str, quality: int | None) -> bytes:
    if image_format == "jpeg" and image.mode == "RGBA":
        with (
            Image.new("RGBA", image.size, "white") as background,
            Image.alpha_composite(background, image) as composite,
            composite.convert("RGB") as flattened,
        ):
            return _save_image(flattened, image_format, quality)
    return _save_image(image, image_format, quality)


def _save_image(image: Image.Image, image_format: str, quality: int | None) -> bytes:
    image.info.clear()
    with BytesIO() as output:
        if image_format == "png":
            image.save(output, format="PNG", compress_level=PNG_COMPRESSION_LEVEL)
        else:
            image.save(
                output,
                format=image_format.upper(),
                quality=quality if quality is not None else DEFAULT_QUALITY,
                max_threads=ENCODER_THREADS,
            )
        return output.getvalue()
