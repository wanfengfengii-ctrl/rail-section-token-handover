import { render, screen, waitFor, within } from "@testing-library/react";
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
      expect(url).toBe("/api/token");
      return jsonResponse(200, { holder: "CONSTRUCTION", version: 0 });
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
        expect(JSON.parse(options.body)).toEqual({
          expected_version: 0,
          target_holder: "TRAFFIC",
        });
        return jsonResponse(200, { holder: "TRAFFIC", version: 1 });
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
