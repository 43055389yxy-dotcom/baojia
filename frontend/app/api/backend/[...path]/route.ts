import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_API_URL ?? "http://127.0.0.1:8000";

type RouteContext = { params: Promise<{ path: string[] }> };

async function forward(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  const joinedPath = path.join("/");
  const isAzurePath = joinedPath.startsWith("api/azure/")
    || joinedPath === "api/azure-product-registry"
    || joinedPath.includes("/azure_")
    || joinedPath.includes("/azure-")
    || request.nextUrl.searchParams.get("provider") === "azure";
  if (isAzurePath) {
    return Response.json(
      { code: "provider_boundary_violation", message: "AWS 报价系统禁止访问 Azure 数据。" },
      { status: 403 },
    );
  }
  const target = new URL(`/${joinedPath}`, BACKEND_URL);
  target.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("origin");
  headers.delete("referer");
  headers.delete("content-length");

  try {
    const body = request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();
    if (body && request.headers.get("content-type")?.includes("application/json")) {
      const payload = JSON.parse(new TextDecoder().decode(body)) as { cloud_provider?: string };
      if (payload.cloud_provider && payload.cloud_provider !== "aws") {
        return Response.json(
          { code: "provider_boundary_violation", message: "AWS 报价系统只接受 AWS 报价任务。" },
          { status: 403 },
        );
      }
    }
    const response = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      cache: "no-store",
    });
    const responseHeaders = new Headers(response.headers);
    responseHeaders.delete("access-control-allow-origin");
    responseHeaders.delete("access-control-allow-credentials");
    // Job, draft and confirmation endpoints are live session state. Never let
    // a browser, framework layer or intermediary replay an older response.
    responseHeaders.set("cache-control", "no-store, no-cache, must-revalidate");
    responseHeaders.set("pragma", "no-cache");
    responseHeaders.set("expires", "0");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    const debugDetails = process.env.NODE_ENV !== "production"
      ? {
          error_type: error instanceof Error ? error.name : typeof error,
          raw_error: error instanceof Error ? error.message : String(error),
          stack: error instanceof Error ? error.stack : undefined,
          backend_target: `${target.origin}/${joinedPath}`,
        }
      : undefined;
    return Response.json(
      {
        code: "backend_unavailable",
        message: "报价服务暂时不可用，请稍后重试。",
        ...(debugDetails ? { details: debugDetails } : {}),
      },
      { status: 503 },
    );
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
