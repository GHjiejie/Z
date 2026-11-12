import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { aborted, ApiClient, type UserRecord } from "./api";

const STORAGE_KEY = "agent-console-session";
function readToken(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}
function storeToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(STORAGE_KEY, token);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* A restricted browser can still keep the session in memory. */
  }
}

export function useSession() {
  const [token, setToken] = useState(readToken);
  const currentToken = useRef(token);
  const [record, setRecord] = useState<UserRecord | null>(null);
  const [permissions, setPermissions] = useState<string[]>([]);
  const [loading, setLoading] = useState(Boolean(token));
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const end = useCallback((message: string) => {
    currentToken.current = null;
    storeToken(null);
    setToken(null);
    setRecord(null);
    setPermissions([]);
    setLoading(false);
    setError(null);
    setNotice(message);
  }, []);
  const expired = useCallback(() => end("登录状态已失效，请重新登录。"), [end]);
  const api = useMemo(
    () =>
      new ApiClient(
        token,
        expired,
        refresh,
        () => currentToken.current === token,
      ),
    [token, expired, refresh],
  );

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setError(null);
    async function load() {
      try {
        const current = await api.me(controller.signal);
        const granted = current.user.must_change_password
          ? []
          : await api.permissions(controller.signal);
        if (!controller.signal.aborted) {
          setRecord(current);
          setPermissions(granted);
          setLoading(false);
        }
      } catch (failure) {
        if (!aborted(failure) && !controller.signal.aborted) {
          setError(failure);
          setLoading(false);
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [api, token, revision]);

  useEffect(() => {
    if (!token) return;
    const focus = () => refresh();
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, [token, refresh]);

  const login = useCallback(
    async (username: string, password: string) => {
      const result = await api.login(username, password);
      currentToken.current = result.session_token;
      storeToken(result.session_token);
      setRecord(null);
      setPermissions([]);
      setError(null);
      setNotice("");
      setLoading(true);
      setToken(result.session_token);
    },
    [api],
  );

  return {
    api,
    token,
    record,
    permissions,
    loading,
    error,
    notice,
    refresh,
    login,
    end,
    setRecord,
  };
}
