import { useRef, useState, type DragEvent } from "react";
import { MediaCard } from "./components/MediaCard";
import { MAX_VIDEO_DURATION_SECONDS, MEDIA_ACCEPT } from "./media/config";
import { formatDuration } from "./media/format";
import { useMediaUpload } from "./media/useMediaUpload";
import { createUploadFormData, getReadyEntries, getVideoDurationSeconds } from "./media/validation";

function UploadIcon() {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <path d="M16 21V5m0 0-6 6m6-6 6 6M7 19v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function App() {
  const { entries, notice, addFiles, removeEntry, retryEntry, clear } = useMediaUpload();
  const inputRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const ready = getReadyEntries(entries);
  const videoSeconds = getVideoDurationSeconds(entries);
  const remaining = Math.max(0, MAX_VIDEO_DURATION_SECONDS - videoSeconds);
  const videoCount = ready.filter((entry) => entry.metadata.kind === "video").length;
  const audioCount = ready.filter((entry) => entry.metadata.kind === "audio").length;
  const isParsing = entries.some((entry) => entry.status === "parsing");
  const failedCount = entries.filter((entry) => entry.status === "error" || entry.status === "rejected").length;
  const canExport = ready.length > 0 && !isParsing;
  const percent = Math.min(100, (videoSeconds / MAX_VIDEO_DURATION_SECONDS) * 100);

  function selectFiles(files: File[]) {
    setExportMessage("");
    addFiles(files);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    selectFiles(Array.from(event.dataTransfer.files));
  }

  function exportResults() {
    try {
      const data = createUploadFormData(entries);
      const metadata = data.get("metadata");
      if (typeof metadata !== "string") throw new Error("未能生成解析结果，请重试。");
      const url = URL.createObjectURL(new Blob([JSON.stringify(JSON.parse(metadata), null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "media-metadata.json";
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setExportMessage(`已导出 ${ready.length} 个有效文件的解析结果。`);
    } catch (error) {
      setExportMessage(error instanceof Error ? error.message : "导出失败，请重试。");
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="./" aria-label="音视频素材台首页">
          <span className="brand-symbol" aria-hidden="true"><i /><i /><i /><i /></span>
          <strong>素材台<span>MEDIA STUDIO</span></strong>
        </a>
        <span className="local-badge"><span aria-hidden="true">●</span> 文件仅在本地解析</span>
      </header>

      <main>
        <div className="intro">
          <span className="eyebrow">上传前的第一步</span>
          <h1>音视频上传与解析<span>。</span></h1>
          <p>批量读取时长与音轨，在上传前完成素材校验。</p>
        </div>

        <div className="workspace">
          <section className="upload-panel" aria-labelledby="upload-title">
            <div className="section-heading"><h2 id="upload-title">添加素材</h2><span>视频 / 音频</span></div>
            <div
              className={`drop-zone ${dragging ? "is-dragging" : ""}`}
              onDragEnter={(event) => { event.preventDefault(); dragDepth.current += 1; setDragging(true); }}
              onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }}
              onDragLeave={(event) => { event.preventDefault(); dragDepth.current = Math.max(0, dragDepth.current - 1); if (dragDepth.current === 0) setDragging(false); }}
              onDrop={onDrop}
            >
              <span className="upload-icon"><UploadIcon /></span>
              <h3>{dragging ? "松开即可添加素材" : "把音视频文件拖到这里"}</h3>
              <p>或从你的设备中选择，支持多文件上传</p>
              <input
                className="visually-hidden"
                ref={inputRef}
                id="media-files"
                type="file"
                accept={MEDIA_ACCEPT}
                multiple
                aria-label="选择音视频文件"
                onChange={(event) => { selectFiles(Array.from(event.currentTarget.files ?? [])); event.currentTarget.value = ""; }}
              />
              <button className="primary-button" type="button" onClick={() => inputRef.current?.click()}>选择文件 <span aria-hidden="true">＋</span></button>
              <span className="formats">MP4、MOV、WebM、MKV · MP3、WAV、M4A、FLAC 等</span>
            </div>
            <div className="upload-tips"><span aria-hidden="true">ⓘ</span><p>多个视频的累计时长最多 <strong>{MAX_VIDEO_DURATION_SECONDS} 秒</strong>；音频文件单独解析，不占用视频时长。</p></div>
          </section>

          <aside className="budget-panel" aria-labelledby="budget-title">
            <div className="section-heading"><h2 id="budget-title">视频时长额度</h2><span className="small-badge">累计计算</span></div>
            <div className="duration-display"><strong data-testid="video-total">{formatDuration(videoSeconds)}</strong><span>/ {MAX_VIDEO_DURATION_SECONDS} 秒</span></div>
            <div className={`duration-meter ${remaining === 0 ? "is-full" : ""}`} role="progressbar" aria-label="已使用视频时长" aria-valuemin={0} aria-valuemax={MAX_VIDEO_DURATION_SECONDS} aria-valuenow={videoSeconds}>
              <span style={{ width: `${percent}%` }} />
            </div>
            <p className="budget-remaining">还可添加 <strong>{formatDuration(remaining)} 秒</strong>视频</p>
            <dl className="budget-stats"><div><dt>已加入视频</dt><dd>{videoCount}<span> 个</span></dd></div><div><dt>已加入音频</dt><dd>{audioCount}<span> 个</span></dd></div></dl>
            <div className="budget-note"><span aria-hidden="true">✓</span><p>通过校验的文件才计入额度。超限或无法读取的文件会保留提示，方便调整后重试。</p></div>
          </aside>
        </div>

        <section className="materials-panel" aria-labelledby="materials-title">
          <div className="materials-heading">
            <div><h2 id="materials-title">素材列表 <span>{entries.length.toString().padStart(2, "0")}</span></h2><p aria-live="polite">{isParsing ? "正在逐个解析文件…" : ready.length > 0 ? `${ready.length} 个文件可用${failedCount > 0 ? `，${failedCount} 个文件未加入` : "，可查看音轨与本地预览"}` : "添加文件后，时长和音轨信息会显示在这里"}</p></div>
            {entries.length > 0 && <button className="text-button" type="button" onClick={() => { clear(); setExportMessage(""); }}>清空列表</button>}
          </div>
          {notice && <p className="notice" role="status">{notice}</p>}
          {entries.length === 0
            ? <div className="empty-state"><span className="empty-icon" aria-hidden="true">♫ <span>▷</span></span><strong>素材准备好了，就从这里开始</strong><p>每段视频的时长、每条音轨的信息，一目了然。</p></div>
            : <div className="media-list">{entries.map((entry) => <MediaCard key={entry.id} entry={entry} onRemove={(id) => { removeEntry(id); setExportMessage(""); }} onRetry={(id) => { retryEntry(id); setExportMessage(""); }} />)}</div>}
          <div className="materials-footer">
            <p role="status">{exportMessage || (isParsing ? "请等待所有文件解析完成" : failedCount > 0 ? "导出仅包含已加入的有效素材" : "解析结果可导出为 JSON，包含时长与全部音轨信息")}</p>
            <button className="export-button" type="button" disabled={!canExport} onClick={exportResults}>{isParsing ? "等待解析完成" : "导出解析结果"}<span aria-hidden="true">↗</span></button>
          </div>
        </section>

        <footer className="page-footer"><span>素材不离开你的设备</span><span>时长校验 · 音轨识别 · 本地预览</span></footer>
      </main>
    </div>
  );
}
