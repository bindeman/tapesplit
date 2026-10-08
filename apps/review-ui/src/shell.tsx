// Window chrome shared by every view: the consolidated toolbar, the
// segmented control with its sliding thumb, and the app icon.
import { createContext, useContext, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ChevronLeft, Inbox, Search } from "lucide-react";

export type ShellActions = {
  openSearch: () => void;
  openReview: () => void;
  reviewCount: number;
};

export const ShellContext = createContext<ShellActions>({
  openSearch: () => undefined,
  openReview: () => undefined,
  reviewCount: 0,
});

// One toolbar per view, pinned to the top of the content column. Titles sit
// left; view controls, search and Review sit right, in that order everywhere.
export function Toolbar({
  title,
  subtitle,
  onBack,
  backLabel,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  onBack?: () => void;
  backLabel?: string;
  children?: ReactNode;
}) {
  const shell = useContext(ShellContext);
  return (
    <header className="toolbar">
      {onBack && (
        <button className="toolbar-back" onClick={onBack} aria-label={backLabel ? `Back to ${backLabel}` : "Back"}>
          <ChevronLeft size={18} />
          {backLabel && <span>{backLabel}</span>}
        </button>
      )}
      <div className="toolbar-title">
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      <div className="toolbar-items">
        {children}
        <button className="toolbar-search" onClick={shell.openSearch} aria-label="Search the archive">
          <Search size={14} />
          <span>Search</span>
          <kbd>⌘K</kbd>
        </button>
        <button
          className="toolbar-icon"
          onClick={shell.openReview}
          aria-label={shell.reviewCount ? `Review, ${shell.reviewCount} waiting` : "Review"}
          title={shell.reviewCount ? `Review · ${shell.reviewCount} waiting` : "Review"}
        >
          <Inbox size={17} />
          {shell.reviewCount > 0 && <em>{shell.reviewCount > 999 ? "999+" : shell.reviewCount}</em>}
        </button>
      </div>
    </header>
  );
}

// A capsule segmented control whose selected pill slides between segments.
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: Array<{ id: T; label: ReactNode }>;
  onChange: (id: T) => void;
  label: string;
}) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [thumb, setThumb] = useState<{ x: number; w: number } | null>(null);

  useLayoutEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const measure = () => {
      const active = track.querySelector<HTMLButtonElement>("button[aria-selected='true']");
      if (active) {
        setThumb({ x: active.offsetLeft, w: active.offsetWidth });
      }
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    return () => observer.disconnect();
  }, [value, options.length]);

  return (
    <div className="segmented" role="tablist" aria-label={label} ref={trackRef}>
      {thumb && <span className="segmented-thumb" style={{ transform: `translateX(${thumb.x}px)`, width: thumb.w }} />}
      {options.map((option) => (
        <button
          key={option.id}
          role="tab"
          aria-selected={value === option.id}
          className={value === option.id ? "active" : ""}
          onClick={() => onChange(option.id)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

// The app icon: a cassette with a photo print tucked into it, drawn the way
// Golden Gate draws icons: flat layers, a crisp outline and real contrast.
export function AppIcon({ size = 30 }: { size?: number }) {
  return (
    <svg className="app-icon" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <defs>
        <linearGradient id="ts-icon-body" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#45454c" />
          <stop offset="1" stopColor="#17171b" />
        </linearGradient>
        <linearGradient id="ts-icon-label" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fffaf0" />
          <stop offset="1" stopColor="#efe5cc" />
        </linearGradient>
        <linearGradient id="ts-icon-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2f8cff" />
          <stop offset="1" stopColor="#b9dcff" />
        </linearGradient>
        <linearGradient id="ts-icon-sheen" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fff" stopOpacity="0.2" />
          <stop offset="0.5" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect x="3" y="3" width="58" height="58" rx="14" fill="url(#ts-icon-body)" />
      <path d="M11 15.5a3.5 3.5 0 0 1 3.5-3.5h35a3.5 3.5 0 0 1 3.5 3.5V31H11z" fill="url(#ts-icon-label)" />
      <path d="M11 15.5a3.5 3.5 0 0 1 3.5-3.5h35a3.5 3.5 0 0 1 3.5 3.5V18H11z" fill="#e2392f" />
      <path d="M16 23.5h22M16 27h14" stroke="#c9bb98" strokeWidth="1.6" strokeLinecap="round" />
      <rect x="11" y="37" width="26" height="14" rx="7" fill="#0c0c0f" stroke="#fff" strokeOpacity="0.14" />
      <circle cx="18" cy="44" r="4.2" fill="#ececf1" />
      <circle cx="18" cy="44" r="1.5" fill="#26262b" />
      <circle cx="30" cy="44" r="4.2" fill="#ececf1" />
      <circle cx="30" cy="44" r="1.5" fill="#26262b" />
      <g transform="rotate(9 47 43)">
        <rect x="36.5" y="31.5" width="21" height="22" rx="1.6" fill="#fff" stroke="#000" strokeOpacity="0.28" strokeWidth="0.8" />
        <rect x="39" y="34" width="16" height="13" fill="url(#ts-icon-sky)" />
        <path d="M39 44.5c2.6-2.6 5.4-3.2 8.2-1.4 2.4 1.5 4.6 1 7.8-1V47H39z" fill="#33a046" />
        <circle cx="51.4" cy="37.6" r="1.9" fill="#ffd34d" />
      </g>
      <rect x="3" y="3" width="58" height="58" rx="14" fill="url(#ts-icon-sheen)" />
      <rect x="3.5" y="3.5" width="57" height="57" rx="13.5" fill="none" stroke="#fff" strokeOpacity="0.12" />
      <rect x="3" y="3" width="58" height="58" rx="14" fill="none" stroke="#000" strokeOpacity="0.45" />
    </svg>
  );
}
