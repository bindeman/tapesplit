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
  // The split cassette: one tape, two halves.
  return (
    <svg className="app-icon" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <defs>
        <linearGradient id="ts-icon-warm" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ff7a45" />
          <stop offset="1" stopColor="#e23b3b" />
        </linearGradient>
        <clipPath id="ts-icon-left">
          <path d="M0 0H35L29 64H0z" />
        </clipPath>
        <clipPath id="ts-icon-right">
          <path d="M37 0H64V64H31z" />
        </clipPath>
        <g id="ts-icon-cassette">
          <rect x="5" y="15" width="54" height="36" rx="6" fill="url(#ts-icon-warm)" />
          <rect x="11" y="20" width="42" height="11" rx="2.2" fill="#fff4e8" />
          <rect x="11" y="20" width="42" height="3.2" rx="1.2" fill="#1d1d1f" />
          <rect x="17" y="35" width="30" height="11" rx="5.5" fill="#3a0f12" />
          <circle cx="24" cy="40.5" r="3.4" fill="#ffe2c4" />
          <circle cx="40" cy="40.5" r="3.4" fill="#ffe2c4" />
        </g>
      </defs>
      <g clipPath="url(#ts-icon-left)" transform="translate(-1.6 1.4) rotate(-3 20 33)">
        <use href="#ts-icon-cassette" />
      </g>
      <g clipPath="url(#ts-icon-right)" transform="translate(1.6 -1.4) rotate(3 44 33)">
        <use href="#ts-icon-cassette" />
      </g>
    </svg>
  );
}
