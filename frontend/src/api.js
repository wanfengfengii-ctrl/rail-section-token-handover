/**
 * 令牌 API 客户端。
 *
 * 并发口径全部在后端保证，前端只做两件事：
 *  1. 每次移交都把“页面当前看到的版本号”作为 expected_version 上送；
 *  2. 收到 409 立即放弃本次意图，重新读取服务器最新状态。
 */

export class ConflictError extends Error {
  constructor(current) {
    super("状态已变化，请重新确认");
    this.name = "ConflictError";
    this.current = current;
  }
}

export class ValidationError extends Error {
  constructor(payload) {
    super("移交请求被拒绝（422）");
    this.name = "ValidationError";
    this.payload = payload;
  }
}

async function parseJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function fetchToken() {
  const response = await fetch("/api/token", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`读取令牌状态失败：HTTP ${response.status}`);
  }
  const data = await response.json();
  return { holder: data.holder, version: data.version };
}

export async function transferToken(expectedVersion, targetHolder) {
  const response = await fetch("/api/token/transfer", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      expected_version: expectedVersion,
      target_holder: targetHolder,
    }),
  });

  if (response.status === 409) {
    const body = await parseJson(response);
    // 抛出携带服务器最新状态的冲突错误，由页面决定如何重新读取与提示。
    throw new ConflictError(body?.current ?? null);
  }

  if (response.status === 422) {
    throw new ValidationError(await parseJson(response));
  }

  if (!response.ok) {
    throw new Error(`移交失败：HTTP ${response.status}`);
  }

  const data = await response.json();
  return { holder: data.holder, version: data.version };
}

export const OTHER_HOLDER = {
  CONSTRUCTION: "TRAFFIC",
  TRAFFIC: "CONSTRUCTION",
};
