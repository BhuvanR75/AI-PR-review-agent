"use client";

/**
 * A minimal data-fetching hook.
 *
 * Deliberately not a data library: the console has six screens and one mutation per
 * screen, and a cache layer would obscure when a review's state is refetched -- which
 * is the one thing a reviewer needs to be sure of before approving anything.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // Held in a ref so the effect can depend on `deps` alone without the caller having
  // to memoise the loader.
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    let active = true;
    setLoading(true);
    loaderRef
      .current()
      .then((value) => {
        if (!active) return;
        setData(value);
        setError(null);
      })
      .catch((exc: unknown) => {
        if (!active) return;
        setError(exc instanceof ApiError ? exc.message : String(exc));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}
