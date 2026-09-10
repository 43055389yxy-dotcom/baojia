import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AstraQuote · 云成本报价",
  description: "AstraQuote 云成本报价平台",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">
        {children}
      </body>
    </html>
  );
}
