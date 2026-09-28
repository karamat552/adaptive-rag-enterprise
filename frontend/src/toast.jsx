import React, {
  createContext, useCallback, useContext, useRef, useState,
} from "react";

// Minimal toast system (~2 KB): a provider, a push function via context,
// and a fixed host stack. No dependency — the "instant toast" the
// signature pass needs for copy/export feedback.
const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);

const TONES = {
  default: "border-ink-600 text-mist-200",
  cyan: "border-cyan-350/40 text-cyan-350",
  emerald: "border-emerald-450/40 text-emerald-450",
  amber: "border-amber-450/40 text-amber-450",
  rose: "border-rose-450/40 text-rose-450",
};

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const seq = useRef(0);

  const push = useCallback((message, tone = "default") => {
    const id = ++seq.current;
    setToasts((t) => [...t.slice(-3), { id, message, tone }]); // max 4
    setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id));
    }, 3400);
  }, []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex
                      max-w-xs flex-col gap-2">
        {toasts.map((t) => (
          <div key={t.id}
               className={`toast glass px-4 py-2.5 text-xs font-medium
                           shadow-lg ${TONES[t.tone] || TONES.default}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
