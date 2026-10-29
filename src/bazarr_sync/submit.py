"""Bounded Bazarr post-processing client: acceptance is not alignment success."""
import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Internal companion URL, e.g. http://companion:8080")
    parser.add_argument("--video", required=True)
    parser.add_argument("--subtitle", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--audio-index", type=int)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be between 0 and 60 seconds")
    body = {"media_path": args.video, "subtitle_path": args.subtitle,
            "language": args.language, "audio_index": args.audio_index}
    request = Request(args.url.rstrip("/") + "/api/jobs", json.dumps(body).encode(),
                      {"Content-Type": "application/json", "X-BBS-Client": "1"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=args.timeout) as response:
            result = json.load(response)
        print("Accepted queued job " + result["id"] + "; alignment has not completed")
        return 0
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError):
        print("Job submission failed; check the internal URL, mount mappings and service status", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
