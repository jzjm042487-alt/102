import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "排料方案对比中心",
  description: "清晰对比两套生产排料方案的利用率、焊口、原管领用与标准化程度。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
