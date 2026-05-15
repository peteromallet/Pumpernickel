export function Header() {
  return (
    <header className="border-b border-white/5 bg-veas-surface/60 backdrop-blur">
      <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-4">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          className="h-7 w-7 text-veas-accent"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="23" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </svg>
        <div className="flex flex-col leading-tight">
          <span className="text-xs uppercase tracking-widest text-veas-muted">
            Veas mediator
          </span>
          <h1 className="text-lg font-semibold text-white">
            Live Voice Agent
          </h1>
        </div>
        <span className="ml-auto hidden text-sm text-veas-muted sm:block">
          Real-time voice conversations with your Veas personas
        </span>
      </div>
    </header>
  );
}
