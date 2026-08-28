import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host");
  const protocol = requestHeaders.get("x-forwarded-proto") ?? "http";
  const origin = host ? `${protocol}://${host}` : "http://localhost:3001";
  const previewImage = new URL("/og.png", origin).toString();
  return {
    title: "AstraQuote · Microsoft Azure 智能报价",
    description: "智能解析客户需求，通过 Microsoft Azure 官方目录生成可追溯的成本估算。",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "AstraQuote · Microsoft Azure 智能报价",
      description: "Microsoft Azure 官方目录选型与核价。",
      images: [{ url: previewImage, width: 1200, height: 630 }],
    },
    twitter: {
      card: "summary_large_image",
      title: "AstraQuote · Microsoft Azure 智能报价",
      description: "Microsoft Azure 官方目录选型与核价。",
      images: [previewImage],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
