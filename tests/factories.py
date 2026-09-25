import subprocess
from io import BytesIO

from PIL import Image


def make_video(path, *, webm=False):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=96x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=10:d=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[out]",
            "-map",
            "[out]",
            "-c:v",
            "libvpx-vp9" if webm else "libx264",
            "-threads",
            "1",
            "-filter_complex_threads",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return path.read_bytes()


def make_image(
    width=120, height=80, mode="RGBA", color=(200, 40, 20, 128), image_format="PNG", **kwargs
):
    with BytesIO() as output, Image.new(mode, (width, height), color) as image:
        image.save(output, format=image_format, **kwargs)
        return output.getvalue()
