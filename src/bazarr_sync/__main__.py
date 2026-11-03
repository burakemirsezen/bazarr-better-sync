from .api import serve
from .config import Config


def main():
    serve(Config.from_env())


if __name__ == "__main__":
    main()
