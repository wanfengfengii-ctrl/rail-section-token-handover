import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";

const CONFLICT_TEXT = "状态已变化，请重新确认";

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setFetchHandler(handler) {
  const mock = vi.fn(handler);
  global.fetch = mock;
  return mock;
}

async function waitUntilLoaded() {
  await screen.findByText(/正在读取令牌状态…/);
  await waitFor(() =>
    expect(screen.queryByText(/正在读取令牌状态…/)).not.toBeInTheDocument()
  );
}

describe("令牌页面", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("首次加载展示当前持有人与单调版本号", async () => {
    setFetchHandler(async (url) => {
      if (url === "/api/token") {
        return jsonResponse(200, { holder: "CONSTRUCTION", version: 0 });
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, []);
      }
      throw new Error(`unexpected url: ${url}`);
    });

    render(<App />);
    await waitUntilLoaded();

    expect(screen.getByTestId("holder")).toHaveTextContent("施工台");
    expect(screen.getByTestId("version")).toHaveTextContent("0");
    // 移交目标必须是相反的一方：行车台。
    expect(screen.getByTestId("transfer-button")).toHaveTextContent(
      "移交给行车台"
    );
  });

  it("移交成功后持有人翻转、版本号加一，且请求带 expected_version", async () => {
    const fetchMock = setFetchHandler(async (url, options = {}) => {
      if (options.method === "POST") {
        // 未填写说明时请求体保持原契约，不携带 handover_note。
        expect(JSON.parse(options.body)).toEqual({
          expected_version: 0,
          target_holder: "TRAFFIC",
        });
        return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, []);
      }
      return jsonResponse(200, { holder: "CONSTRUCTION", version: 0 });
    });

    render(<App />);
    await waitUntilLoaded();

    await userEvent.click(screen.getByTestId("transfer-button"));

    await waitFor(() =>
      expect(screen.getByTestId("holder")).toHaveTextContent("行车台")
    );
    expect(screen.getByTestId("version")).toHaveTextContent("1");
    // 下一次移交的目标翻转回施工台。
    expect(screen.getByTestId("transfer-button")).toHaveTextContent(
      "移交给施工台"
    );
    expect(screen.queryByTestId("conflict-notice")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/token/transfer",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("收到 409 后重新读取最新状态、取消本次意图并提示冲突文案", async () => {
    // 另一浏览器在本页面加载后完成了移交；GET 在移交发生后返回最新状态。
    let transferred = false;
    const getCalls = vi.fn(async () =>
      jsonResponse(
        200,
        transferred
          ? { holder: "TRAFFIC", version: 1 }
          : { holder: "CONSTRUCTION", version: 0 }
      )
    );

    const fetchMock = setFetchHandler(async (url, options = {}) => {
      if (url === "/api/token" && options.method !== "POST") {
        return getCalls();
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, []);
      }
      // 旧页面拿着 version 0 提交：版本已被另一浏览器推进到 1。
      transferred = true;
      return jsonResponse(409, {
        detail: CONFLICT_TEXT,
        current: { holder: "TRAFFIC", version: 1 },
      });
    });

    render(<App />);
    await waitUntilLoaded();

    // 首次加载的状态。
    expect(screen.getByTestId("version")).toHaveTextContent("0");

    await userEvent.click(screen.getByTestId("transfer-button"));

    // 出现规定提示文案。
    const notice = await screen.findByTestId("conflict-notice");
    expect(notice).toHaveTextContent(CONFLICT_TEXT);

    // 页面读取了服务器最新状态：版本来自服务器（1），不是本地乐观加一。
    await waitFor(() =>
      expect(screen.getByTestId("version")).toHaveTextContent("1")
    );
    expect(screen.getByTestId("holder")).toHaveTextContent("行车台");
    // 冲突后至少重新 GET 了一次（首次加载一次 + 冲突后刷新一次）。
    expect(getCalls.mock.calls.length).toBeGreaterThanOrEqual(2);

    // 本次意图已取消：按钮恢复可点，目标按最新状态计算为施工台。
    await waitFor(() =>
      expect(screen.getByTestId("transfer-button")).not.toBeDisabled()
    );
    expect(screen.getByTestId("transfer-button")).toHaveTextContent(
      "移交给施工台"
    );

    // POST 只发生了一次（意图没有被自动重试）。
    const postCalls = fetchMock.mock.calls.filter(
      ([, options]) => options?.method === "POST"
    );
    expect(postCalls).toHaveLength(1);
  });

  it("两个浏览器同版本先后移交：失败者显示对手结果，双方不可能同时持有", async () => {
    // 浏览器 A：移交成功；浏览器 B：同一版本 0 的请求收到 409。
    const states = { A: null, B: null };

    function makeHandler(browser) {
      return async (url, options = {}) => {
        if (options.method === "POST") {
          if (browser === "A") {
            states.A = { holder: "TRAFFIC", version: 1 };
            return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
          }
          return jsonResponse(409, {
            detail: CONFLICT_TEXT,
            current: { holder: "TRAFFIC", version: 1 },
          });
        }
        if (url === "/api/token/handovers") {
          return jsonResponse(200, []);
        }
        // B 冲突后重新读取，看到的是 A 提交后的唯一状态。
        return jsonResponse(200, states.A ?? { holder: "CONSTRUCTION", version: 0 });
      };
    }

    const a = document.createElement("div");
    const b = document.createElement("div");
    document.body.append(a, b);

    // 用全局变量切换当前点击来自哪个“浏览器”。
    let pendingBrowser = "A";
    const fetchMock = setFetchHandler((url, options) =>
      pendingBrowser === "A"
        ? makeHandler("A")(url, options)
        : makeHandler("B")(url, options)
    );

    const utilsA = render(<App />, { container: a });
    const utilsB = render(<App />, { container: b });
    await waitFor(() => expect(a.textContent).toContain("施工台"));
    await waitFor(() => expect(b.textContent).toContain("施工台"));

    // A 先移交成功。
    await userEvent.click(within(a).getByTestId("transfer-button"));
    await waitFor(() =>
      expect(within(a).getByTestId("version")).toHaveTextContent("1")
    );
    expect(within(a).getByTestId("holder")).toHaveTextContent("行车台");

    // B 仍基于旧版本 0 提交。
    pendingBrowser = "B";
    await userEvent.click(within(b).getByTestId("transfer-button"));

    await waitFor(() =>
      expect(within(b).getByTestId("conflict-notice")).toHaveTextContent(
        CONFLICT_TEXT
      )
    );
    // B 放弃意图并与服务器对齐：持有人同样显示行车台，而不是自己以为的新版本。
    expect(within(b).getByTestId("holder")).toHaveTextContent("行车台");
    expect(within(b).getByTestId("version")).toHaveTextContent("1");

    // 两边展示的唯一持有人一致，不存在“双方都持有”。
    expect(
      within(a).getByTestId("holder").textContent ===
        within(b).getByTestId("holder").textContent
    ).toBe(true);

    utilsA.unmount();
    utilsB.unmount();
    fetchMock.mockRestore();
  });
});

describe("移交记录", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("带说明移交成功后，记录区立即展示从哪台到哪台、版本、说明与记录时间", async () => {
    const record = {
      from_holder: "CONSTRUCTION",
      to_holder: "TRAFFIC",
      version: 1,
      note: "施工结束，区间空闲",
      created_at: "2026-09-15T10:20:30.123456+00:00",
    };
    let transferred = false;

    const fetchMock = setFetchHandler(async (url, options = {}) => {
      if (options.method === "POST") {
        // 填写的说明随请求上送。
        expect(JSON.parse(options.body)).toEqual({
          expected_version: 0,
          target_holder: "TRAFFIC",
          handover_note: "施工结束，区间空闲",
        });
        transferred = true;
        return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, transferred ? [record] : []);
      }
      return jsonResponse(
        200,
        transferred
          ? { holder: "TRAFFIC", version: 1 }
          : { holder: "CONSTRUCTION", version: 0 }
      );
    });

    render(<App />);
    await waitUntilLoaded();

    // 初始没有记录。
    expect(screen.getByTestId("records-empty")).toBeInTheDocument();

    await userEvent.type(
      screen.getByTestId("note-input"),
      "施工结束，区间空闲"
    );
    await userEvent.click(screen.getByTestId("transfer-button"));

    await waitFor(() =>
      expect(screen.getByTestId("holder")).toHaveTextContent("行车台")
    );

    // 记录区同步刷新，完整展示最新一条移交记录。
    const list = await screen.findByTestId("records-list");
    expect(list).toHaveTextContent("从施工台交给行车台");
    expect(list).toHaveTextContent("版本 1");
    expect(list).toHaveTextContent("施工结束，区间空闲");
    expect(list).toHaveTextContent("2026-09-15 10:20:30");

    // 说明输入框在成功后清空，便于下一次填写。
    expect(screen.getByTestId("note-input")).toHaveValue("");

    // 成功后确实重新拉取了记录接口。
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => url === "/api/token/handovers")
          .length
      ).toBeGreaterThanOrEqual(2)
    );
  });

  it("手动刷新同步更新记录区", async () => {
    const record = {
      from_holder: "TRAFFIC",
      to_holder: "CONSTRUCTION",
      version: 2,
      note: null,
      created_at: "2026-09-15T11:00:00+00:00",
    };
    let recordVisible = false;

    setFetchHandler(async (url, options = {}) => {
      if (options.method === "POST") {
        throw new Error("本用例不发生移交");
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, recordVisible ? [record] : []);
      }
      return jsonResponse(200, { holder: "TRAFFIC", version: 2 });
    });

    render(<App />);
    await waitUntilLoaded();
    expect(screen.getByTestId("records-empty")).toBeInTheDocument();

    // 另一浏览器完成了移交；本页手动刷新后记录区同步展示。
    recordVisible = true;
    await userEvent.click(screen.getByTestId("refresh-button"));

    const list = await screen.findByTestId("records-list");
    expect(list).toHaveTextContent("从行车台交给施工台");
    expect(list).toHaveTextContent("版本 2");
    expect(list).toHaveTextContent("2026-09-15 11:00:00");
  });

  it("说明含表情符号时按字符计数，200 字可完整输入并上送", async () => {
    let submittedNote = null;
    setFetchHandler(async (url, options = {}) => {
      if (options.method === "POST") {
        submittedNote = JSON.parse(options.body).handover_note;
        return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(200, []);
      }
      return jsonResponse(200, { holder: "CONSTRUCTION", version: 0 });
    });

    render(<App />);
    await waitUntilLoaded();

    const input = screen.getByTestId("note-input");

    // 200 个表情符号：按码点算是 200 字，必须完整保留（maxLength 会误判为 400）。
    fireEvent.change(input, { target: { value: "😀".repeat(200) } });
    expect([...input.value].length).toBe(200);

    // 超出 200 字的部分被截断，而不是整段丢弃。
    fireEvent.change(input, { target: { value: "😀".repeat(201) } });
    expect([...input.value].length).toBe(200);

    // 中文与表情混合：199 个汉字 + 1 个表情 = 200 字。
    fireEvent.change(input, { target: { value: "施".repeat(199) + "😀" } });
    expect([...input.value].length).toBe(200);

    // 200 字说明原样随移交请求上送。
    await userEvent.click(screen.getByTestId("transfer-button"));
    await waitFor(() =>
      expect(screen.getByTestId("holder")).toHaveTextContent("行车台")
    );
    expect(submittedNote).toBe("施".repeat(199) + "😀");
  });

  it("记录接口故障只在记录区提示，令牌状态与移交操作不受影响", async () => {
    let transferred = false;

    setFetchHandler(async (url, options = {}) => {
      if (options.method === "POST") {
        expect(JSON.parse(options.body)).toEqual({
          expected_version: 0,
          target_holder: "TRAFFIC",
        });
        transferred = true;
        return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
      }
      if (url === "/api/token/handovers") {
        return jsonResponse(500, { detail: "boom" });
      }
      return jsonResponse(
        200,
        transferred
          ? { holder: "TRAFFIC", version: 1 }
          : { holder: "CONSTRUCTION", version: 0 }
      );
    });

    render(<App />);
    await waitUntilLoaded();

    // 记录区提示故障，但令牌状态正常展示。
    expect(await screen.findByTestId("records-error")).toHaveTextContent(
      "读取移交记录失败"
    );
    expect(screen.getByTestId("holder")).toHaveTextContent("施工台");
    expect(screen.getByTestId("version")).toHaveTextContent("0");

    // 主流程仍可用：移交成功、持有人翻转、版本加一。
    await userEvent.click(screen.getByTestId("transfer-button"));
    await waitFor(() =>
      expect(screen.getByTestId("holder")).toHaveTextContent("行车台")
    );
    expect(screen.getByTestId("version")).toHaveTextContent("1");

    // 故障没有扩散成整页错误，仍只停留在记录区。
    expect(screen.getByTestId("records-error")).toBeInTheDocument();
    expect(screen.getByTestId("transfer-button")).toHaveTextContent(
      "移交给施工台"
    );
  });
});
