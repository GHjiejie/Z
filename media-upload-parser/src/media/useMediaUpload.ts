import { useCallback, useEffect, useRef, useState } from "react";
import { MediaParser } from "./parser";
import type { MediaEntry } from "./types";
import { validateAddition } from "./validation";

export function useMediaUpload() {
  const [entries, setEntries] = useState<MediaEntry[]>([]);
  const [notice, setNotice] = useState("");
  const entriesRef = useRef<MediaEntry[]>([]);
  const parserRef = useRef<MediaParser | null>(null);
  const controllersRef = useRef(new Map<string, AbortController>());
  const mountedRef = useRef(false);

  const update = useCallback((next: MediaEntry[]) => {
    entriesRef.current = next;
    if (mountedRef.current) setEntries(next);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const controllers = controllersRef.current;
    return () => {
      mountedRef.current = false;
      for (const controller of controllers.values()) controller.abort();
      controllers.clear();
      parserRef.current?.dispose();
      parserRef.current = null;
    };
  }, []);

  const parseEntry = useCallback(async (entry: MediaEntry) => {
    const controller = new AbortController();
    controllersRef.current.set(entry.id, controller);
    const parser = parserRef.current ?? new MediaParser();
    parserRef.current = parser;
    try {
      const metadata = await parser.parse(entry.file, controller.signal);
      if (!mountedRef.current || controller.signal.aborted) return;
      if (!entriesRef.current.some((item) => item.id === entry.id)) return;
      // Synchronous ref commits prevent separate selection batches from sharing
      // the same stale remaining-duration budget.
      const error = validateAddition(entriesRef.current, metadata);
      const next: MediaEntry = error
        ? { ...entry, status: "rejected", metadata, error }
        : { ...entry, status: "ready", metadata };
      update(entriesRef.current.map((item) => item.id === entry.id ? next : item));
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted) return;
      const message = error instanceof Error ? error.message : "解析失败，请重试。";
      update(entriesRef.current.map((item) => item.id === entry.id
        ? { id: entry.id, file: entry.file, status: "error", error: message }
        : item));
    } finally {
      if (controllersRef.current.get(entry.id) === controller) controllersRef.current.delete(entry.id);
    }
  }, [update]);

  const addFiles = useCallback((files: File[]) => {
    const added: MediaEntry[] = [];
    let duplicates = 0;
    for (const file of files) {
      const duplicate = [...entriesRef.current, ...added].some((entry) =>
        entry.file.name === file.name && entry.file.size === file.size &&
        entry.file.lastModified === file.lastModified);
      if (duplicate) {
        duplicates += 1;
        continue;
      }
      added.push({ id: crypto.randomUUID(), file, status: "parsing" });
    }
    setNotice(duplicates > 0 ? `已跳过 ${duplicates} 个重复文件；需要重新解析时，请点击文件上的“重试”。` : "");
    update([...entriesRef.current, ...added]);
    for (const entry of added) void parseEntry(entry);
  }, [parseEntry, update]);

  const removeEntry = useCallback((id: string) => {
    controllersRef.current.get(id)?.abort();
    controllersRef.current.delete(id);
    update(entriesRef.current.filter((entry) => entry.id !== id));
    setNotice("");
  }, [update]);

  const clear = useCallback(() => {
    for (const controller of controllersRef.current.values()) controller.abort();
    controllersRef.current.clear();
    parserRef.current?.dispose();
    parserRef.current = null;
    update([]);
    setNotice("");
  }, [update]);

  const retryEntry = useCallback((id: string) => {
    const entry = entriesRef.current.find((item) => item.id === id);
    if (!entry || (entry.status !== "error" && entry.status !== "rejected")) return;
    const next: MediaEntry = { id: entry.id, file: entry.file, status: "parsing" };
    update(entriesRef.current.map((item) => item.id === id ? next : item));
    void parseEntry(next);
  }, [parseEntry, update]);

  return { entries, notice, addFiles, removeEntry, retryEntry, clear };
}
