import { useEffect, useState } from "react";
import { formatDuration, formatSize } from "../media/format";
import type { MediaEntry, MediaMetadata } from "../media/types";

function MediaPreview({ file, metadata }: { file: File; metadata: MediaMetadata }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    setFailed(false);
    return () => URL.revokeObjectURL(next);
  }, [file]);

  if (failed) {
    return <p className="preview-warning">浏览器暂不能播放此文件，时长和音轨信息仍可查看。</p>;
  }
  if (!url) return null;
  return metadata.kind === "video"
    ? <video src={url} controls preload="metadata" playsInline onError={() => setFailed(true)} aria-label={`${file.name} 视频预览`} />
    : <audio src={url} controls preload="metadata" onError={() => setFailed(true)} aria-label={`${file.name} 音频预览`} />;
}

function TrackDetails({ metadata }: { metadata: MediaMetadata }) {
  return (
    <div className="track-details">
      {metadata.videoTracks.map((track, index) => (
        <div className="track-row" key={`${track.id}-${index}`}>
          <span className="track-label">视频轨道 {index + 1}</span>
          <span>{track.codec}</span>
          {track.width !== null && track.height !== null && <span>{track.width} × {track.height}</span>}
          {track.frameRate !== null && <span>{track.frameRate} fps</span>}
        </div>
      ))}
      <h4>音轨信息 <span>{metadata.audioTracks.length}</span></h4>
      {metadata.audioTracks.length === 0
        ? <p className="no-tracks">此视频没有音轨。</p>
        : metadata.audioTracks.map((track, index) => (
          <div className="audio-track" key={`${track.id}-${index}`}>
            <div className="audio-track-heading">
              <strong>音轨 {index + 1}{track.title ? ` · ${track.title}` : ""}</strong>
              {track.isDefault && <span className="small-badge">默认</span>}
            </div>
            <dl className="track-fields">
              <div><dt>编码</dt><dd>{track.codec}</dd></div>
              <div><dt>声道</dt><dd>{track.channels === null ? "未提供" : `${track.channels} 声道`}</dd></div>
              <div><dt>采样率</dt><dd>{track.sampleRate === null ? "未提供" : `${track.sampleRate / 1000} kHz`}</dd></div>
              <div><dt>码率</dt><dd>{track.bitRate === null ? "未提供" : `${(track.bitRate / 1000).toFixed(1)} kbps`}</dd></div>
              <div><dt>语言</dt><dd>{track.language ?? "未标注"}</dd></div>
              <div><dt>时长</dt><dd>{track.durationSeconds === null ? "未提供" : `${formatDuration(track.durationSeconds)} 秒`}</dd></div>
            </dl>
          </div>
        ))}
    </div>
  );
}

export function MediaCard({ entry, onRemove, onRetry }: {
  entry: MediaEntry;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}) {
  const metadata = entry.status === "ready" || entry.status === "rejected" ? entry.metadata : null;
  const isError = entry.status === "error" || entry.status === "rejected";
  const status = {
    parsing: "解析中",
    ready: "已加入",
    rejected: "超过时长限制",
    error: "解析失败",
  }[entry.status];

  return (
    <article className={`media-card ${isError ? "has-error" : ""}`} aria-label={entry.file.name}>
      <div className="media-card-top">
        <div className={`file-symbol ${metadata?.kind === "audio" ? "is-audio" : ""}`} aria-hidden="true">
          {entry.status === "parsing" ? <span className="spinner" /> : metadata?.kind === "audio" ? "♫" : "▷"}
        </div>
        <div className="file-heading">
          <h3 title={entry.file.name}>{entry.file.name}</h3>
          <p>{formatSize(entry.file.size)}{metadata && ` · ${metadata.kind === "video" ? "视频" : "音频"} · ${metadata.container}`}</p>
        </div>
        <span className={`status-badge status-${entry.status}`}>{status}</span>
        <button className="icon-button" type="button" onClick={() => onRemove(entry.id)} aria-label={`移除 ${entry.file.name}`} title="移除文件">×</button>
      </div>

      {metadata && (
        <div className="file-metrics">
          <span><strong>{formatDuration(metadata.durationSeconds)}</strong> 秒</span>
          <span>{metadata.audioTracks.length} 条音轨</span>
          {metadata.kind === "audio" && <span>不计入视频时长</span>}
        </div>
      )}
      {entry.status === "parsing" && <p className="parsing-note">正在本地读取媒体时长与音轨…</p>}
      {isError && (
        <div className="file-error" role="alert">
          <p>{entry.error}</p>
          <button type="button" onClick={() => onRetry(entry.id)}>重试</button>
        </div>
      )}
      {metadata && (
        <details className="file-details">
          <summary>查看音轨与预览</summary>
          <TrackDetails metadata={metadata} />
          <MediaPreview file={entry.file} metadata={metadata} />
        </details>
      )}
    </article>
  );
}
