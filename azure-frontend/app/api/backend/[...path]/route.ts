import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_API_URL ?? "http://127.0.0.1:8001";

type RouteContext = { params: Promise<{ path: string[] }> };

async function forward(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  const joinedPath = path.join("/");
  const isAwsPath = joinedPath.startsWith("api/aws/")
    || joinedPath === "api/aws-product-registry"
    || joinedPath.includes("/aws_")
    || joinedPath.includes("/aws-")
    || request.nextUrl.searchParams.get("provider") === "aws";
  if (isAwsPath) {
    return Response.json(
      { code: "provider_boundary_violation", message: "Microsoft Azure 报价系统禁止访问 AWS 数据。" },
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
      if (payload.cloud_provider && payload.cloud_provider !== "azure") {
        return Response.json(
          { code: "provider_boundary_violation", message: "Microsoft Azure 报价系统只接受 Azure 报价任务。" },
          { status: 403 },
        );
      }
    }
    const response = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
    });
    const responseHeaders = new Headers(response.headers);
    responseHeaders.delete("access-control-allow-origin");
    responseHeaders.delete("access-control-allow-credentials");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      { code: "backend_unavailable", message: "报价服务暂时不可用，请稍后重试。" },
      { status: 503 },
    );
  }
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
