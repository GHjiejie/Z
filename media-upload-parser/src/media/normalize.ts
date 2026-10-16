import type { MediaMetadata } from "./types";

type Track = Record<string, unknown>;

function isRecord(value: unknown): value is Track {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function positiveNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function trackId(track: Track, index: number): string {
  const id = track.ID;
  return typeof id === "number" ? String(id) : text(id) ?? String(index + 1);
}

export function normalizeMediaInfo(result: unknown): MediaMetadata {
  if (!isRecord(result) || !isRecord(result.media) || !Array.isArray(result.media.track)) {
    throw new Error("未能读取媒体信息，请检查文件是否完整。");
  }

  const tracks = result.media.track.filter(isRecord);
  const general = tracks.find((track) => track["@type"] === "General");
  const videos = tracks.filter((track) => track["@type"] === "Video");
  const audios = tracks.filter((track) => track["@type"] === "Audio");
  if (videos.length === 0 && audios.length === 0) {
    throw new Error("文件中没有可识别的视频或音频轨道，请选择有效的音视频文件。");
  }

  // Cover art is not a video stream: an MP3 with an Image track remains audio.
  // MediaInfo object/JSON Duration is in seconds, not the text output's milliseconds.
  // Use the longest known duration so a short video stream cannot hide a longer audio stream.
  const durations = [general, ...videos, ...audios]
    .map((track) => positiveNumber(track?.Duration))
    .filter((duration): duration is number => duration !== null);
  if (durations.length === 0) {
    throw new Error("无法确定文件的有效时长，暂不能加入。请尝试重新导出该文件。");
  }

  return {
    kind: videos.length > 0 ? "video" : "audio",
    container: text(general?.Format) ?? "未知格式",
    durationSeconds: Math.max(...durations),
    audioTracks: audios.map((track, index) => ({
      id: trackId(track, index),
      codec: text(track.Format) ?? text(track.CodecID) ?? "未知编码",
      language: text(track.Language),
      title: text(track.Title),
      channels: positiveNumber(track.Channels),
      sampleRate: positiveNumber(track.SamplingRate),
      bitRate: positiveNumber(track.BitRate),
      durationSeconds: positiveNumber(track.Duration),
      isDefault: track.Default === "Yes",
    })),
    videoTracks: videos.map((track, index) => ({
      id: trackId(track, index),
      codec: text(track.Format) ?? text(track.CodecID) ?? "未知编码",
      width: positiveNumber(track.Width),
      height: positiveNumber(track.Height),
      frameRate: positiveNumber(track.FrameRate),
      durationSeconds: positiveNumber(track.Duration),
    })),
  };
}
