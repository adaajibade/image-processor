import json
import sys

from .config import Config
from .errors import ApiError
from .options import ProcessOptions
from .transform import transform


def main() -> None:
    header = json.loads(sys.stdin.buffer.readline())
    try:
        result = transform(
            sys.stdin.buffer.read(),
            ProcessOptions(**header["options"]),
            Config(**header["config"]),
        )
        metadata = {
            "content_type": result.content_type,
            "width": result.width,
            "height": result.height,
        }
        data = result.data
    except ApiError as error:
        metadata = {"error": {"status": error.status, "code": error.code, "message": error.message}}
        data = b""
    sys.stdout.buffer.write(json.dumps(metadata).encode() + b"\n" + data)


if __name__ == "__main__":
    main()
