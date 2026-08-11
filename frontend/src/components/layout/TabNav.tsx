import { LayoutDashboard, Network, Search, Settings2 } from "lucide-react";
import { NavLink } from "react-router";
import { cn } from "@/lib/utils";

/** Primary application destinations. Routing is the state — no local state. */
const TABS = [
  { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { to: "/sessions", label: "Sessions", icon: Network },
  { to: "/search", label: "Search", icon: Search },
  { to: "/settings", label: "Settings", icon: Settings2 },
] as const;

export function TabNav() {
  return (
    <nav
      aria-label="Main"
      className="ml-3 flex min-w-0 items-center gap-0.5 md:mt-5 md:ml-0 md:w-full md:flex-col md:gap-1"
    >
      {TABS.map((tab) => {
        const Icon = tab.icon;
        return (
          <NavLink
            aria-label={tab.label}
            key={tab.to}
            to={tab.to}
            className={({ isActive }) =>
              cn(
                "group relative flex h-9 items-center gap-2 rounded-sm px-2.5 text-meta transition-colors duration-[120ms] ease-out md:h-[52px] md:w-full md:flex-col md:justify-center md:gap-1 md:px-1 md:text-badge",
                isActive
                  ? "bg-elevated text-accent before:absolute before:inset-y-2 before:left-0 before:w-px before:bg-accent md:before:inset-x-3 md:before:top-auto md:before:bottom-0 md:before:h-px md:before:w-auto"
                  : "text-fg-muted hover:bg-elevated/70 hover:text-fg",
              )
            }
          >
            <Icon
              aria-hidden="true"
              className="size-4 shrink-0"
              strokeWidth={1.75}
            />
            <span className="hidden sm:inline">{tab.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}
