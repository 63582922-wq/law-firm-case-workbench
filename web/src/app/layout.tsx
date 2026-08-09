import type { Metadata } from "next";
import { DesktopBridgeInstaller } from "@/components/desktop-bridge-installer";
import "./globals.css";

export const metadata: Metadata = {
  title: "律所案件 AI 工作台 · 内部合成 Alpha",
  description: "中文优先的案件材料核验工作台原型。",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body>
        <DesktopBridgeInstaller />
        {children}
      </body>
    </html>
  );
}
