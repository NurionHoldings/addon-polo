const REQUEST_HEADERS = ["accept", "content-type", "cookie", "user-agent"];
const RESPONSE_HEADERS = ["content-type", "content-disposition", "cache-control", "location"];

export default async (request) => {
  const rawOrigin = Netlify.env.get("ONBUILDING_BACKEND_URL");
  if (!rawOrigin) {
    return new Response("운영 서버 연결을 준비 중입니다.", {
      status: 503,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    });
  }

  let backend;
  try {
    backend = new URL(rawOrigin);
  } catch {
    return new Response("운영 서버 주소 설정을 확인해 주세요.", { status: 500 });
  }
  const localDev = ["localhost", "127.0.0.1"].includes(backend.hostname);
  if ((!localDev && backend.protocol !== "https:") || backend.username || backend.password ||
      backend.pathname !== "/" || backend.search || backend.hash) {
    return new Response("운영 서버 주소 설정을 확인해 주세요.", { status: 500 });
  }

  const incoming = new URL(request.url);
  if (incoming.pathname.startsWith("/.netlify/")) {
    return new Response("Not found", { status: 404 });
  }
  const target = new URL(`${incoming.pathname}${incoming.search}`, backend.origin);
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: "manual",
      signal: AbortSignal.timeout(55_000),
    });
    const responseHeaders = new Headers({ "cache-control": "no-store" });
    for (const name of RESPONSE_HEADERS) {
      const value = upstream.headers.get(name);
      if (!value) continue;
      if (name === "location" && value.startsWith(backend.origin)) {
        responseHeaders.set(name, value.slice(backend.origin.length) || "/");
      } else {
        responseHeaders.set(name, value);
      }
    }
    const setCookies = upstream.headers.getSetCookie?.() ?? [];
    for (const cookie of setCookies) responseHeaders.append("set-cookie", cookie);
    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch {
    return new Response("운영 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.", {
      status: 502,
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    });
  }
};

export const config = { path: "/*", preferStatic: true };
