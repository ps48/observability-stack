import { useState, useEffect } from 'react';

const taglines = [
  // Traces (cyan)
  { label: 'Traces', text: 'Distributed tracing with auto-generated service maps and RED metrics', color: 'text-cyan-400' },
  { label: 'Traces', text: 'OpenTelemetry-native. No proprietary agent required', color: 'text-cyan-400' },
  // Logs (amber)
  { label: 'Logs', text: 'Full-text search + PPL structured queries across billions of log events', color: 'text-amber-400' },
  { label: 'Logs', text: 'Correlate logs with traces in one click. No context switching', color: 'text-amber-400' },
  // AI Agents (orange)
  { label: 'AI Agents', text: 'Trace LLM calls, tool use, and agent workflows end-to-end', color: 'text-orange-400' },
  { label: 'AI Agents', text: 'GenAI semantic conventions, MCP support, token cost tracking', color: 'text-orange-400' },
  // PPL (violet)
  { label: 'PPL', text: 'ML-powered log clustering in one command. No regex, no ML expertise', color: 'text-violet-400' },
  { label: 'PPL', text: 'Anomaly detection built into queries — no external ML service needed', color: 'text-violet-400' },
  // Metrics (emerald)
  { label: 'Metrics', text: 'Prometheus-compatible with PromQL. RED metrics computed automatically', color: 'text-emerald-400' },
  { label: 'Metrics', text: 'Custom dashboards with real-time panels and alerting', color: 'text-emerald-400' },
  // Open Source (green)
  { label: 'Open', text: 'Apache 2.0. No feature gates. No lock-in', color: 'text-green-400' },
  { label: 'Open', text: 'Self-host anywhere. Your data stays yours', color: 'text-green-400' },
];

export function CyclingTagline() {
  const [index, setIndex] = useState(0);
  const [isVisible, setIsVisible] = useState(true);

  useEffect(() => {
    const interval = setInterval(() => {
      setIsVisible(false);
      setTimeout(() => {
        setIndex((prev) => (prev + 1) % taglines.length);
        setIsVisible(true);
      }, 400);
    }, 3000);

    return () => clearInterval(interval);
  }, []);

  const current = taglines[index];

  return (
    <div className="h-8 flex items-center relative">
      <span className={`${current.color} text-sm font-semibold uppercase tracking-wide whitespace-nowrap w-[100px]`}>
        {current.label}
      </span>
      <div className="absolute left-[100px] top-0 h-full flex items-center overflow-hidden" style={{ width: 'calc(100vw - 200px)', maxWidth: '500px' }}>
        <span
          className={`text-slate-400 text-sm whitespace-nowrap transition-opacity duration-500 ease-in-out ${
            isVisible ? 'opacity-100' : 'opacity-0'
          }`}
        >
          {current.text}
        </span>
      </div>
    </div>
  );
}
