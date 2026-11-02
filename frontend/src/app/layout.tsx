import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "PR Review Console",
  description:
    "Approve or reject AI pull-request reviews before they post, and see exactly what each pass did.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="shell">
          <span className="brand">PR Review Console</span>
          <nav>
            <Link href="/connect">Review a PR</Link>
            <Link href="/">Approval queue</Link>
            <Link href="/economics">Economics</Link>
            <Link href="/agents">Agents</Link>
          </nav>
          <span className="muted" style={{ marginLeft: "auto", fontSize: 13 }}>
            Nothing posts to GitHub until a human approves it.
          </span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
