import { describe, expect, it } from "vitest";
import { normalizeMediaInfo } from "./normalize";

const result = (track: Record<string, unknown>[]) => ({ media: { track } });

describe("media metadata normalization", () => {
  it("reads duration in seconds and keeps all audio tracks with separate channel counts", () => {
    const metadata = normalizeMediaInfo(result([
      { "@type": "General", Format: "MPEG-4", Duration: 12.5 },
      { "@type": "Video", Format: "AVC", Duration: 12.5, Width: 1920, Height: 1080, FrameRate: 25 },
      { "@type": "Audio", ID: "2", Format: "AAC", Channels: 2, SamplingRate: 48000, BitRate: 128000, Duration: 12.5, Language: "en", Default: "Yes" },
      { "@type": "Audio", ID: "3", Format: "AC-3", Channels: 6, SamplingRate: 48000, Language: "zh", Title: "中文" },
    ]));
    expect(metadata.kind).toBe("video");
    expect(metadata.durationSeconds).toBe(12.5);
    expect(metadata.audioTracks).toHaveLength(2);
    expect(metadata.audioTracks[0]).toMatchObject({ codec: "AAC", channels: 2, sampleRate: 48000, isDefault: true });
    expect(metadata.audioTracks[1]).toMatchObject({ channels: 6, language: "zh", title: "中文", durationSeconds: null });
    expect(metadata.videoTracks[0]).toMatchObject({ width: 1920, height: 1080, frameRate: 25 });
  });

  it("takes the longest media duration to reject a video with a longer audio stream", () => {
    expect(normalizeMediaInfo(result([
      { "@type": "General", Duration: 20 },
      { "@type": "Video", Duration: 20 },
      { "@type": "Audio", Duration: 31 },
    ])).durationSeconds).toBe(31);
  });

  it("parses silent video without inventing an audio track", () => {
    expect(normalizeMediaInfo(result([{ "@type": "Video", Duration: "10.25" }])).audioTracks).toEqual([]);
  });

  it("keeps an audio file with cover art classified as audio", () => {
    expect(normalizeMediaInfo(result([
      { "@type": "General", Duration: 120, Format: "MPEG Audio" },
      { "@type": "Image", Format: "JPEG" },
      { "@type": "Audio", Channels: 2 },
    ])).kind).toBe("audio");
  });

  it("rejects invalid contents and missing or non-finite duration", () => {
    for (const value of [null, {}, result([]), result([{ "@type": "Image", Duration: 2 }]), result([{ "@type": "Video" }])]) {
      expect(() => normalizeMediaInfo(value)).toThrow();
    }
    for (const Duration of [NaN, Infinity, "Infinity", "", 0, -5, true]) {
      expect(() => normalizeMediaInfo(result([{ "@type": "Video", Duration }]))).toThrow("时长");
    }
  });
});
