# Sigma Brewer

A small self-hosted web UI for downloading YouTube videos that you own or are authorized to download. Built with FastAPI, yt-dlp and FFmpeg.

## Deploy

Railway builds the included Dockerfile. The app listens on `$PORT` and exposes `/health`.

## Notes

- Supports 360p–1080p where available; 720p is the default.
- One active download at a time to protect the service from overload.
- Temporary output is stored in the container's temp directory and removed after the download response completes. It is not durable storage.
- A 12 GB maximum output size is configured.
- YouTube availability, rate limits, and network throughput determine actual speed. No service can guarantee a particular download speed.
- Use only for content you own or have permission to download, and comply with applicable terms and laws.
