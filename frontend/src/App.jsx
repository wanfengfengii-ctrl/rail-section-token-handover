import { useCallback, useEffect, useRef, useState } from "react";
import {
  ConflictError,
  OTHER_HOLDER,
  fetchToken,
  transferToken,
} from "./api.js";

const HOLDER_LABELS = {
  CONSTRUCTION: "施工台",
  TRAFFIC: "行车台",
};

const CONFLICT_NOTICE = "状态已变化，请重新确认";

export default function App() {
  const [token, setToken] = useState(null);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState("");
  const [notice, setNotice] = useState("");
  const [transferring, setTransferring] = useState(false);
  // 本次移交意图使用发起时看到的版本号；409 后意图作废。
  const intentVersionRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const latest = await fetchToken();
      setToken(latest);
      setPageError("");
    } catch (err) {
      setPageError(err.message);
    }
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const latest = await fetchToken();
        if (active) {
          setToken(latest);
          setLoading(false);
        }
      } catch (err) {
        if (active) {
          setPageError(err.message);
          setLoading(false);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // 两个浏览器同时操作时，定时拉取让另一侧的移交结果可见（不改变并发口径）。
  useEffect(() => {
    const timer = setInterval(() => {
      if (intentVersionRef.current === null) refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleTransfer = async () => {
    if (!token || transferring) return;
    const expectedVersion = token.version;
    const target = OTHER_HOLDER[token.holder];
    intentVersionRef.current = expectedVersion;
    setTransferring(true);
    setNotice("");
    try {
      const next = await transferToken(expectedVersion, target);
      setToken(next);
    } catch (err) {
      if (err instanceof ConflictError) {
        // 版本已被他人推进：读取服务器最新状态、取消本次意图、提示。
        await refresh();
        setNotice(CONFLICT_NOTICE);
      } else {
        setPageError(err.message);
      }
    } finally {
      intentVersionRef.current = null;
      setTransferring(false);
    }
  };

  const holderLabel = token ? HOLDER_LABELS[token.holder] ?? token.holder : "";
  const targetLabel = token
    ? HOLDER_LABELS[OTHER_HOLDER[token.holder]]
    : "";

  return (
    <main className="page">
      <h1>区间占用令牌</h1>

      {loading && <p role="status">正在读取令牌状态…</p>}

      {pageError && (
        <p className="error" role="alert">
          {pageError}
        </p>
      )}

      {token && (
        <section className="card" aria-label="令牌状态">
          <dl>
            <div>
              <dt>当前持有人</dt>
              <dd data-testid="holder">{holderLabel}</dd>
            </div>
            <div>
              <dt>版本号</dt>
              <dd data-testid="version">{token.version}</dd>
            </div>
          </dl>

          <button
            type="button"
            data-testid="transfer-button"
            onClick={handleTransfer}
            disabled={transferring}
          >
            {transferring
              ? "移交中…"
              : `移交令牌（移交给${targetLabel}）`}
          </button>

          <button
            type="button"
            data-testid="refresh-button"
            onClick={() => {
              setNotice("");
              refresh();
            }}
            disabled={transferring}
          >
            刷新状态
          </button>

          {notice && (
            <p className="notice" role="alert" data-testid="conflict-notice">
              {notice}
            </p>
          )}
        </section>
      )}
    </main>
  );
}
