import { mkdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export default function generateFixtures() {
  const directory = fileURLToPath(new URL("./fixtures/", import.meta.url));
  mkdirSync(directory, { recursive: true });
  const ffmpeg = process.env.FFMPEG_PATH || "ffmpeg";
  function generate(name: string, args: string[]) {
    const result = spawnSync(ffmpeg, ["-hide_banner", "-loglevel", "error", "-y", ...args, `${directory}/${name}`], { encoding: "utf8" });
    if (result.error || result.status !== 0) {
      throw new Error(`无法生成测试素材，请安装 FFmpeg 或设置 FFMPEG_PATH。${result.error?.message ?? result.stderr}`);
    }
  }
  function silentVideo(name: string, seconds: number) {
    generate(name, ["-f", "lavfi", "-i", `color=c=0x6355d9:s=160x90:r=25:d=${seconds}`, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]);
  }
  silentVideo("video-20.mp4", 20);
  silentVideo("video-10.mp4", 10);
  silentVideo("video-30.mp4", 30);
  silentVideo("video-over.mp4", 30.04);
  generate("audio-40.wav", ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=40", "-c:a", "pcm_s16le"]);
  generate("audio-stereo.wav", ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=3", "-ac", "2", "-c:a", "pcm_s16le"]);
  generate("video-multi-audio.mp4", [
    "-f", "lavfi", "-i", "color=c=0x6355d9:s=160x90:r=25:d=5",
    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=5",
    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=5",
    "-map", "0:v", "-map", "1:a", "-map", "2:a",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2",
    "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=zho",
    "-movflags", "+faststart",
  ]);
  generate("video.webm", [
    "-f", "lavfi", "-i", "color=c=0x6355d9:s=160x90:r=25:d=3",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
    "-c:v", "libvpx-vp9", "-deadline", "realtime", "-c:a", "libopus",
  ]);
  writeFileSync(`${directory}/corrupt.mp4`, "This is not a video file.");
  writeFileSync(`${directory}/empty.mp4`, "");
}
