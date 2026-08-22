"""Entry point kept at the path the Docker image and cron job expect."""

from scraper.run import main

if __name__ == "__main__":
    raise SystemExit(main())
