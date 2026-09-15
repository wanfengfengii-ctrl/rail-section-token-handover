import { useCallback, useEffect, useRef, useState } from "react";
import {
  ConflictError,
  OTHER_HOLDER,
  fetchHandovers,
  fetchToken,
  transferToken,
} from "./api.js";

const HOLDER_LABELS = {
  CONSTRUCTION: "施工台",
  TRAFFIC: "行车台",
};

const CONFLICT_NOTICE = "状态已变化，请重新确认";
const NOTE_MAX_LENGTH = 200;

// 与服务端 max_length 同口径：按 Unicode 码点计数字数。
// 表情符号等在 UTF-16 中是代理对，String.length / maxLength 会算成 2，
// 用码点迭代才能保证“200 字”对表情符号也成立。
function clampNote(value) {
  return [...value].slice(0, NOTE_MAX_LENGTH).join("");
}

function holderLabel(holder) {
  return HOLDER_LABELS[holder] ?? holder;
}

function formatRecordTime(iso) {
  // 服务端返回 UTC ISO 串；去掉 T 与微秒部分，便于阅读与核对。
  return typeof iso === "string"
    ? iso.replace("T", " ").replace(/\.\d+/, "")
    : "";
}

export default function App() {
  const [token, setToken] = useState(null);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState("");
  const [notice, setNotice] = useState("");
  const [transferring, setTransferring] = useState(false);
  const [note, setNote] = useState("");
  // 移交记录是辅助信息：故障只在记录区提示，不影响令牌主流程。
  const [records, setRecords] = useState([]);
  const [recordsError, setRecordsError] = useState("");
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

  const refreshRecords = useCallback(async () => {
    try {
      const latest = await fetchHandovers();
      setRecords(latest);
      setRecordsError("");
    } catch (err) {
      setRecordsError(err.message);
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
    // 初次加载同步记录区；失败只在记录区提示。
    refreshRecords();
    return () => {
      active = false;
    };
  }, [refreshRecords]);

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
      const next = await transferToken(expectedVersion, target, note);
      setToken(next);
      setNote("");
      // 成功后立即刷新记录区，展示刚写入的这条移交记录。
      await refreshRecords();
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

  const holderText = token ? holderLabel(token.holder) : "";
  const targetLabel = token ? holderLabel(OTHER_HOLDER[token.holder]) : "";

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
              <dd data-testid="holder">{holderText}</dd>
            </div>
            <div>
              <dt>版本号</dt>
              <dd data-testid="version">{token.version}</dd>
            </div>
          </dl>

          <label className="note-field">
            <span>移交说明（可选，不超过 {NOTE_MAX_LENGTH} 字）</span>
            <textarea
              data-testid="note-input"
              rows={2}
              value={note}
              placeholder="例如：施工结束，区间空闲"
              disabled={transferring}
              onChange={(event) => setNote(clampNote(event.target.value))}
            />
          </label>

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
              refreshRecords();
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

      <section className="card" aria-label="移交记录">
        <h2>最近移交记录</h2>

        {recordsError && (
          <p className="error" role="alert" data-testid="records-error">
            {recordsError}
          </p>
        )}

        {!recordsError && records.length === 0 && (
          <p className="muted" data-testid="records-empty">
            暂无移交记录
          </p>
        )}

        {records.length > 0 && (
          <ul className="records" data-testid="records-list">
            {records.map((record) => (
              <li key={record.version} className="record">
                <div className="record-main">
                  <span data-testid={`record-route-${record.version}`}>
                    从{holderLabel(record.from_holder)}交给
                    {holderLabel(record.to_holder)}
                  </span>
                  <span className="record-version">
                    版本 {record.version}
                  </span>
                </div>
                {record.note && (
                  <p className="record-note">{record.note}</p>
                )}
                <time dateTime={record.created_at}>
                  {formatRecordTime(record.created_at)}
                </time>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
