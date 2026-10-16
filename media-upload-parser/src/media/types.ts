export type MediaKind = "video" | "audio";

export interface AudioTrackInfo {
  id: string;
  codec: string;
  language: string | null;
  title: string | null;
  channels: number | null;
  sampleRate: number | null;
  bitRate: number | null;
  durationSeconds: number | null;
  isDefault: boolean;
}

export interface VideoTrackInfo {
  id: string;
  codec: string;
  width: number | null;
  height: number | null;
  frameRate: number | null;
  durationSeconds: number | null;
}

export interface MediaMetadata {
  kind: MediaKind;
  container: string;
  durationSeconds: number;
  audioTracks: AudioTrackInfo[];
  videoTracks: VideoTrackInfo[];
}

export interface ParseRequest {
  id: string;
  file: File;
}

export type ParseResponse =
  | { id: string; ok: true; metadata: MediaMetadata }
  | { id: string; ok: false; error: string };

interface EntryBase {
  id: string;
  file: File;
}

export type MediaEntry =
  | (EntryBase & { status: "parsing" })
  | (EntryBase & { status: "ready"; metadata: MediaMetadata })
  | (EntryBase & { status: "rejected"; metadata: MediaMetadata; error: string })
  | (EntryBase & { status: "error"; error: string });

export type ReadyMediaEntry = Extract<MediaEntry, { status: "ready" }>;
