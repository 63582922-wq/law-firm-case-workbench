import type { Metadata } from "next";
import { DesktopBridgeInstaller } from "@/components/desktop-bridge-installer";
import "./globals.css";

export const metadata: Metadata = {
  title: "律师办案工作台",
  description: "本机优先的律师案件材料、法律依据、利息测算与应诉材料工作台。",
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
