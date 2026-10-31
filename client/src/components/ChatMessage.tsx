import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown, ChevronRight, User, Brain, FileText, Maximize2, Minimize2, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, type Message, type TurnMeta } from "@/lib/api";
import { UiButton, UiFormItem, UiIconButton } from "@/components/ui";

interface ChatMessageProps {
  message: Message;
  isStreaming?: boolean;
  /** Turns ahead of this one while it waits for a concurrency slot, else null. */
  queuedAhead?: number | null;
  sessionId?: string | null;
}

// --- Report card ---
// The document opens in its own tab rather than an iframe: it carries its own
// "Save as PDF" button, and printing a page through an iframe is unreliable.
function ReportCard({ reportId }: Readonly<{ reportId: string }>) {
  return (
    <a
      href={`/api/reports/${reportId}`}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-3 my-2 p-3 rounded-button border border-turbine_lens-primary/30 bg-turbine_lens-primary/5 hover:bg-turbine_lens-primary/10 transition no-underline"
    >
      <FileText size={20} className="text-turbine_lens-primary shrink-0" />
      <span className="flex flex-col">
        <span className="text-sm font-medium text-gray-900">Session report</span>
        <span className="text-xs text-gray-500">
          Opens in a new tab · use “Save as PDF” there to download
        </span>
      </span>
    </a>
  );
}

// --- Chart + report fence rendering ---
// Detects ```plotly\n<path>\n```, ```echarts\n<path>\n``` and ```report\n<id>\n``` fences.
// Charts become iframes on the /charts/ endpoint; a report becomes a link card.
function renderContentWithCharts(content: string): React.ReactNode[] {
  const chartFence = /```(plotly|echarts|report)[^\S\n]*\n([^]*?)\n```/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = chartFence.exec(content)) !== null) {
    // Text before the fence
    if (match.index > lastIndex) {
      parts.push(
        <ReactMarkdown key={key++} remarkPlugins={[remarkGfm]}>
          {content.slice(lastIndex, match.index)}
        </ReactMarkdown>
      );
    }
    const raw = match[2].trim();
    if (match[1] === "report") {
      if (raw) parts.push(<ReportCard key={key++} reportId={raw} />);
    } else if (raw.startsWith("/")) {
      const filename = raw.split("/").pop()?.replace(".json", ".html") || "";
      parts.push(<ChartFrame key={key++} filename={filename} />);
    }
    lastIndex = match.index + match[0].length;
  }

  // Remaining text after last fence
  if (lastIndex < content.length) {
    parts.push(
      <ReactMarkdown key={key++} remarkPlugins={[remarkGfm]}>
        {content.slice(lastIndex)}
      </ReactMarkdown>
    );
  }

  return parts.length > 0 ? parts : [<ReactMarkdown key={0} remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>];
}

// --- Resizable chart frame ---
const CHART_MIN_H = 280;
const CHART_MAX_H = 1400;
const CHART_DEFAULT_H = 480;
const CHART_PREF_KEY = "fm.chartHeight";
const CHART_PRESETS = [
  { label: "S", h: 360 },
  { label: "M", h: 520 },
  { label: "L", h: 760 },
];
const clampH = (h: number) => Math.max(CHART_MIN_H, Math.min(CHART_MAX_H, Math.round(h)));

function savedHeight(): number {
  const v = Number(localStorage.getItem(CHART_PREF_KEY));
  return v >= CHART_MIN_H && v <= CHART_MAX_H ? v : CHART_DEFAULT_H;
}

function ChartFrame({ filename }: Readonly<{ filename: string }>) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState<number>(savedHeight);
  const heightRef = useRef(height);
  heightRef.current = height;
  const userSized = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [isFull, setIsFull] = useState(false);

  const commit = (h: number) => {
    const v = clampH(h);
    userSized.current = true;
    setHeight(v);
    localStorage.setItem(CHART_PREF_KEY, String(v));
  };

  // The chart page reports its preferred height (its `_height`, or one sized to a
  // ranking's row count). It can only grow the frame, never override a manual resize.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (e.origin !== window.location.origin) return;
      if (e.source !== frameRef.current?.contentWindow) return;
      if (e.data?.type !== "fm-chart-height" || userSized.current) return;
      const h = Number(e.data.height);
      if (h > 0) setHeight((cur) => Math.max(cur, clampH(h)));
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  useEffect(() => {
    const onFs = () => setIsFull(document.fullscreenElement === wrapRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  const startDrag = (e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = heightRef.current;
    userSized.current = true;
    setDragging(true); // the iframe would otherwise swallow pointermove
    const move = (ev: PointerEvent) => setHeight(clampH(startH + ev.clientY - startY));
    const up = () => {
      setDragging(false);
      localStorage.setItem(CHART_PREF_KEY, String(heightRef.current));
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const onHandleKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); commit(heightRef.current + 40); }
    if (e.key === "ArrowUp") { e.preventDefault(); commit(heightRef.current - 40); }
  };

  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else wrapRef.current?.requestFullscreen?.();
  };

  const src = `/charts/${filename}`;
  return (
    <div className="my-2">
      <div
        ref={wrapRef}
        className="relative bg-white rounded-button overflow-hidden"
        style={{ height: isFull ? "100vh" : height }}
      >
        <iframe
          ref={frameRef}
          src={src}
          title="Chart"
          className="w-full h-full border-none"
          style={{ pointerEvents: dragging ? "none" : "auto" }}
        />
        {isFull && (
          <button
            type="button"
            onClick={toggleFull}
            aria-label="Exit full screen"
            className="absolute top-2 left-2 p-1.5 rounded-button bg-white/90 shadow text-gray-700"
          >
            <Minimize2 size={16} />
          </button>
        )}
      </div>

      <div className="flex items-center gap-2 mt-1 text-xs text-gray-500">
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize chart"
          aria-valuemin={CHART_MIN_H}
          aria-valuemax={CHART_MAX_H}
          aria-valuenow={height}
          tabIndex={0}
          onPointerDown={startDrag}
          onKeyDown={onHandleKey}
          title="Drag to resize (or ↑/↓)"
          className="flex-1 h-2 rounded-full bg-gray-200 hover:bg-turbine_lens-primary/40 focus:bg-turbine_lens-primary/40 cursor-row-resize touch-none"
        />
        {CHART_PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => commit(p.h)}
            aria-pressed={height === p.h}
            className={cn(
              "px-1.5 rounded border",
              height === p.h ? "border-turbine_lens-primary text-turbine_lens-primary" : "border-gray-300 hover:border-turbine_lens-primary"
            )}
          >
            {p.label}
          </button>
        ))}
        <button type="button" onClick={toggleFull} aria-label="Full screen" title="Full screen" className="hover:text-gray-800">
          <Maximize2 size={14} />
        </button>
        <a href={src} target="_blank" rel="noopener noreferrer" aria-label="Open in new tab" title="Open in new tab" className="hover:text-gray-800">
          <ExternalLink size={14} />
        </a>
      </div>
    </div>
  );
}

// --- Cost line component ---
function CostLine({ meta }: Readonly<{ meta: TurnMeta }>) {
  if (!meta.cost_usd && meta.cost_usd !== 0) return null;
  const bits: string[] = [];
  if (meta.cost_usd != null) bits.push(`💰 LLM ≈ $${meta.cost_usd.toFixed(3)}`);
  if (meta.duration_ms != null) bits.push(`⏱ ${(meta.duration_ms / 1000).toFixed(1)}s`);
  if (meta.genie_count) bits.push(`🧞 ${meta.genie_count} Genie`);
  if (meta.sql_count) bits.push(`🔧 ${meta.sql_count} SQL`);
  return (
    <div
      /* Pas d'`opacity` sur du texte : elle rabat une encre déjà calibrée sous le
         seuil AA. Mesuré : `opacity-70` sur cette encre donnait 3,43:1 en clair et
         4,08:1 en sombre, contre 4,5 exigés. L'atténuation doit venir de l'encre
         (échelle `text-gray-*`, adossée aux tokens), jamais de l'alpha. */
      className="text-xs text-gray-500 mt-2"
      title="LLM token estimate (Anthropic list pricing). Genie and SQL-warehouse compute billed separately."
    >
      {bits.join(" · ")}
    </div>
  );
}

// --- Feedback component ---
// Same reason set as the Gradio UI, so historical feedback stays comparable.
const FEEDBACK_REASONS = [
  "wrong data", "missing data", "misunderstood", "wrong entity", "too slow", "other",
];

function FeedbackButtons({ message, sessionId }: { message: Message; sessionId?: string | null }) {
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const [submitted, setSubmitted] = useState(false);
  /** Which vote is awaiting an optional comment. Both 👍 and 👎 open the form —
   *  praise is as useful as criticism, and only offering the box on 👎 meant a user
   *  who liked an answer had no way to say why. */
  const [askingWhy, setAskingWhy] = useState<"up" | "down" | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [comment, setComment] = useState("");

  const send = async (value: "up" | "down", why?: string | null, note?: string) => {
    setRating(value);
    setSubmitted(true);
    setAskingWhy(null);
    try {
      await api.submitFeedback({
        session_id: sessionId || undefined,
        trace_id: message.trace_id,
        level: "answer",
        rating: value,
        reason: why || undefined,
        comment: note?.trim() || undefined,
      });
    } catch {
      // silent — feedback is best-effort
    }
  };

  // Deliberately NOT gated on message.trace_id. Tracing can fail silently (MLflow
  // hands back a NoOp span under concurrency), and gating on it meant those answers
  // could never be rated — losing the feedback precisely when something had gone
  // wrong. Without a trace the rating still records against the session; only the
  // MLflow assessment is skipped, server-side.
  if (submitted) {
    return (
      <span className="text-xs text-gray-400 ml-2">
        {rating === "up" ? "👍" : "👎"} Thanks
      </span>
    );
  }

  // Either vote opens this form, and is written once with its comment attached —
  // rather than writing the bare rating first and enriching it afterwards, which
  // would need a second row or a second MLflow assessment for the same answer.
  if (askingWhy) {
    const isDown = askingWhy === "down";
    return (
      <div className="mt-2 rounded-button border border-gray-200 bg-gray-50 p-3">
        <p className="text-xs font-medium text-gray-700 mb-2">
          {isDown ? "What was off?" : "What worked well?"}{" "}
          <span className="font-normal text-gray-400">(optional — helps Turbine Lens improve)</span>
        </p>
        <div className={cn("flex flex-wrap gap-1.5", isDown ? "mb-2" : "hidden")}>
          {FEEDBACK_REASONS.map((r) => (
            <button type="button"
              key={r}
              onClick={() => setReason(reason === r ? null : r)}
              className={cn(
                "px-2 py-1 rounded-button text-xs border transition",
                reason === r
                  ? "bg-turbine_lens-primary text-inverse border-turbine_lens-primary"
                  : "bg-surface text-gray-600 border-gray-300 hover:border-turbine_lens-primary"
              )}
            >
              {r}
            </button>
          ))}
        </div>
        <UiFormItem
          id="feedback-comment"
          name="feedback-comment"
          isMultiline
          rows={2}
          label="Why?"
          labelKind="static"
          placeholder={isDown ? "What went wrong? (optional)" : "What was good? (optional)"}
          value={comment}
          onChange={(e) => setComment((e.target as HTMLTextAreaElement).value)}
          className="[&_label]:sr-only"
        />
        <div className="flex items-center gap-2 mt-2">
          <UiButton
            onClick={() => send(askingWhy, isDown ? reason : null, comment)}
            label="Submit feedback"
            variant="primary"
            emphasis="bold"
            scale="xs"
          />
          <UiButton
            onClick={() => send(askingWhy)}
            label="Skip"
            variant="primary"
            emphasis="minimal"
            scale="xs"
          />
        </div>
      </div>
    );
  }

  return (
    // Always visible. Hover-only controls were invisible on touch devices and easy to
    // miss entirely — the Gradio UI showed them permanently, and their apparent
    // disappearance after the migration was really this.
    <div className="flex items-center gap-1 mt-1 opacity-70 hover:opacity-100 transition-opacity">
      <UiIconButton
        onClick={() => setAskingWhy("up")}
        icon="thumb_up"
        variant="secondary"
        scale="2xs"
        title="Good answer"
        aria-label="Good answer"
      />
      <UiIconButton
        onClick={() => setAskingWhy("down")}
        icon="thumb_down"
        variant="secondary"
        scale="2xs"
        title="Bad answer"
        aria-label="Bad answer"
      />
    </div>
  );
}

// --- Main ChatMessage component ---
export function ChatMessage({ message, isStreaming, queuedAhead, sessionId }: Readonly<ChatMessageProps>): React.ReactElement {
  const [stepsOpen, setStepsOpen] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const isUser = message.role === "user";
  const steps = message.steps_json || [];
  const thinking = message.thinking || "";
  const meta = message.meta;
  const content = message.content || "";
  const hasChart = !isUser && /```(plotly|echarts)/.test(content);

  // Separate cost line from steps (last step starting with 💰)
  const costStep = steps.length > 0 && steps[steps.length - 1].startsWith("💰") ? steps[steps.length - 1] : null;
  const toolSteps = costStep ? steps.slice(0, -1) : steps;

  return (
    <div className={cn("group flex gap-3 py-4", isUser && "flex-row-reverse")}>
      {/* Avatar */}
      {isUser ? (
        <div className="w-8 h-8 bg-gray-200 rounded-button flex items-center justify-center shrink-0">
          <User size={16} className="text-gray-600" />
        </div>
      ) : (
        <div className="w-8 h-8 bg-turbine_lens-primary rounded-button flex items-center justify-center shrink-0">
          <span className="text-inverse text-xs font-bold">FM</span>
        </div>
      )}

      {/* Content */}
      <div
        className={cn(
          "rounded-2xl px-4 py-3 min-w-0",
          // 2.75rem = avatar (2rem) + gap (0.75rem)
          hasChart ? "w-full max-w-[calc(100%-2.75rem)]" : "max-w-[75%]",
          isUser ? "bg-turbine_lens-primary text-inverse" : "bg-gray-100 text-gray-900"
        )}
      >
        {/* Steps panel */}
        {!isUser && toolSteps.length > 0 && (
          <div className="mb-2">
            <button
              type="button"
              onClick={() => setStepsOpen(!stepsOpen)}
              aria-expanded={stepsOpen}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 transition"
            >
              {stepsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              <span className="font-medium">
                🔎 Steps ({toolSteps.length})
              </span>
            </button>
            {stepsOpen && (
              <ul className="mt-1 ml-4 space-y-0.5 text-xs text-gray-600">
                {toolSteps.map((step, i) => (
                  <li key={i} className="leading-relaxed">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        p: ({ children }) => <span>{children}</span>,
                      }}
                    >
                      {step}
                    </ReactMarkdown>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Cost line (from meta or from step) */}
        {!isUser && meta && <CostLine meta={meta} />}
        {!isUser && !meta && costStep && (
          <div className="text-xs text-gray-500 mb-2">{costStep}</div>
        )}

        {/* Thinking panel */}
        {!isUser && thinking && (
          <div className="mb-2">
            <button
              type="button"
              onClick={() => setThinkingOpen(!thinkingOpen)}
              aria-expanded={thinkingOpen}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 transition"
            >
              {thinkingOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              <Brain size={12} />
              <span className="font-medium">Thoughts</span>
            </button>
            {thinkingOpen && (
              // Genie's research plan, queries and report arrive as Markdown (built in
              // routers/agent.py `_emit_genie_thinking`). No rehype-raw: HTML stays escaped.
              <div className="chat-message-markdown thinking-markdown mt-1 ml-4 text-xs text-gray-600 max-h-96 overflow-y-auto">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{thinking}</ReactMarkdown>
              </div>
            )}
          </div>
        )}

        {/* Main content */}
        <div className={cn(
          "chat-message-markdown",
          isUser && "text-inverse [&_code]:bg-white/20",
          isStreaming && content && "streaming-caret"
        )}>
          {renderContentWithCharts(content)}
        </div>

        {/* Streaming indicator */}
        {isStreaming && !content && (
          <div className="flex items-center gap-2 py-1">
            <div className="flex gap-1">
              <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce" />
              <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce [animation-delay:0.15s]" />
              <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce [animation-delay:0.3s]" />
            </div>
            <span className="text-sm text-turbine_lens-primary font-medium">
              {queuedAhead
                ? `Waiting for a free slot — ${queuedAhead} turn${queuedAhead > 1 ? "s" : ""} ahead…`
                : "Turbine Lens is thinking…"}
            </span>
          </div>
        )}
        {isStreaming && content && (
          <>
            <span className="inline-block w-1.5 h-4 bg-turbine_lens-primary animate-pulse ml-0.5 rounded" />
            {/* Keep an explicit "still working" signal next to partial text. After a
                page reload the replayed answer makes `content` non-empty immediately,
                so with only the caret the turn looks finished while tools are still
                running — which is exactly when the user needs to know it isn't. */}
            <div className="flex items-center gap-2 py-1 mt-2">
              <div className="flex gap-1">
                <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce" />
                <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce [animation-delay:0.15s]" />
                <div className="w-1.5 h-1.5 bg-turbine_lens-primary rounded-full animate-bounce [animation-delay:0.3s]" />
              </div>
              <span className="text-xs text-turbine_lens-primary font-medium">
                Turbine Lens is still working…
              </span>
            </div>
          </>
        )}

        {/* Feedback (assistant messages only) */}
        {!isUser && !isStreaming && (
          <FeedbackButtons message={message} sessionId={sessionId} />
        )}
      </div>
    </div>
  );
}
