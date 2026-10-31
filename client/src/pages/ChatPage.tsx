import type React from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Star, Calculator } from "lucide-react";
import { toast } from "sonner";
import { api, streamProgress, type ActiveExecution, type AppConfig, type GenieMode, type Message, type ReportVersion, type SessionSummary, type StreamEvent, type TurnMeta, type UserInfo } from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";
import { ChatMessage } from "@/components/ChatMessage";
import { ProductLockup } from "@/components/ProductLockup";
import { ThemeToggle } from "@/components/ThemeToggle";
import { SidebarResizer, SIDEBAR_MIN, SIDEBAR_MAX } from "@/components/SidebarResizer";
import { UiButton, UiFormItem, UiIconButton, UiInputSearch, UiSegmentedControl, UiSegmentedControlButton } from "@/components/ui";

/** Mirrors MAX_TITLE_LEN in routers/conversations.py, which is authoritative — this
 *  only stops the input promising more than the server will keep. */
const SESSION_TITLE_MAX = 120;

export function ChatPage(): React.ReactElement {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  /** Which session the in-flight turn belongs to. Streaming state is global to this
   *  component, so without this the preview leaks into every session you open. */
  const [streamingSessionId, setStreamingSessionId] = useState<string | null>(null);
  /** Turns ahead of ours while waiting for a concurrency slot, else null. */
  const [streamingQueued, setStreamingQueued] = useState<number | null>(null);
  const [streamingText, setStreamingText] = useState("");
  const [streamingSteps, setStreamingSteps] = useState<string[]>([]);
  const [streamingThinking, setStreamingThinking] = useState("");
  const [streamingMeta, setStreamingMeta] = useState<TurnMeta | undefined>();
  const [executionId, setExecutionId] = useState<string | null>(null);
  const [sessionRating, setSessionRating] = useState<number | null>(null);
  const [sessionComment, setSessionComment] = useState("");
  const [sessionFeedbackSent, setSessionFeedbackSent] = useState(false);
  const [user, setUser] = useState<UserInfo | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  /** Largeur de la barre latérale, en pixels. Mémorisée localement pour la même
   *  raison que le thème : un réglage qui repart à zéro à chaque chargement donne
   *  l'impression que la poignée ne sert à rien. */
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    try {
      const v = Number(localStorage.getItem("turbine_lens.sidebarWidth"));
      if (v >= SIDEBAR_MIN && v <= SIDEBAR_MAX) return v;
    } catch {
      /* stockage indisponible (navigation privée stricte) : largeur par défaut */
    }
    return 320; // w-80, la valeur d'origine
  });
  useEffect(() => {
    try {
      localStorage.setItem("turbine_lens.sidebarWidth", String(sidebarWidth));
    } catch {
      /* non bloquant : la largeur reste appliquée pour la session en cours */
    }
  }, [sidebarWidth]);
  /** Session whose title is being edited in place, and the text being typed. */
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [genieMode, setGenieMode] = useState<GenieMode>("agent");
  const [scopeFilter, setScopeFilter] = useState<"mine" | "all">("all");
  const [readOnly, setReadOnly] = useState(false);
  const [sessionOwner, setSessionOwner] = useState<string | null>(null);
  /** Report versions for the open session. Immutable, so this list only grows. */
  const [reports, setReports] = useState<ReportVersion[]>([]);
  const [reportBusy, setReportBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  /** Execution this tab is already streaming, so reattach never doubles it up. */
  const ownStreamRef = useRef<string | null>(null);
  /** Mirror of activeSessionId readable inside long-lived event closures, which
   *  would otherwise capture a stale value for the whole turn. */
  const activeSessionIdRef = useRef<string | null>(null);
  activeSessionIdRef.current = activeSessionId;
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // The composer grows with its content, up to 40% of the viewport, then scrolls.
  // Layout effect, so the new height is applied before paint (no one-frame jump).
  // Runs on every `input` change, which also shrinks it back once a turn is sent.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round(window.innerHeight * 0.4);
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);
  /** Whether new content should pull the view down. False as soon as the user
   *  scrolls up to re-read something, so a streaming answer stops fighting them. */
  const stickToBottomRef = useRef(true);
  /** Session the last scroll was performed for, so opening a conversation is told
   *  apart from following one. */
  const scrolledForSessionRef = useRef<string | null>(null);

  // Load user identity and sessions on mount
  useEffect(() => {
    api.getUser().then(setUser).catch(() => {});
    api.listSessions().then(setSessions).catch(() => {});
    api.getGenieMode().then((r) => setGenieMode(r.mode)).catch(() => {}); // global default on mount
    api.getConfig().then(setConfig).catch(() => {});
  }, []);
  // Filtered sessions (search + mine/all)
  const filteredSessions = sessions.filter((s) => {
    if (scopeFilter === "mine" && user?.email && s.owner_email !== user.email) return false;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      return (s.title || "").toLowerCase().includes(q) ||
             (s.owner_email || "").toLowerCase().includes(q);
    }
    return true;
  });

  // A turn streams into component-level state, so the preview, the Stop button and
  // the completed message must all be scoped to the session that turn belongs to —
  // otherwise they show up in whatever conversation happens to be open.
  const showStream = isStreaming && streamingSessionId === activeSessionId;
  const otherSessionStreaming = isStreaming && !showStream;

  // Auto-scroll. Two rules, and both exist because tokens arrive for a turn the
  // user may not be looking at:
  //   1. Follow the *rendered* preview, not `streamingText`. That state is updated
  //      for every text event whatever session is open (deliberately — it must be
  //      current when the user comes back), so keying the scroll off it dragged the
  //      conversation being read down on every token of a turn in another session.
  //   2. Only follow while the user is already at the bottom, so scrolling up to
  //      re-read an earlier answer isn't undone by the answer still being written.
  // Declared after `showStream` because it depends on it.
  const visibleStreamText = showStream ? streamingText : "";

  // Only a real scroll can change this. Content growing under a view already at the
  // bottom fires no scroll event, so the follow is never disarmed by the answer it
  // is following.
  const handleScrollAreaScroll = useCallback(() => {
    const el = scrollAreaRef.current;
    if (!el) return;
    // 100px of slack: fractional layout heights mean "at the bottom" is never
    // exactly scrollHeight.
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
  }, []);

  useEffect(() => {
    // activeSessionId is read but not a dependency on purpose: the trigger is the
    // new transcript arriving (`messages`), which always lands in a render that
    // already carries the new id. Depending on it too would fire this once against
    // the *previous* session's messages and animate the switch.
    if (scrolledForSessionRef.current !== activeSessionId) {
      scrolledForSessionRef.current = activeSessionId;
      stickToBottomRef.current = true; // a freshly opened conversation follows again
      messagesEndRef.current?.scrollIntoView({ behavior: "auto" }); // a jump, not a trip
      return;
    }
    if (!stickToBottomRef.current) return;
    // Instant while a stream is being followed, smooth for a discrete message.
    // Not cosmetic: a smooth scroll animates *towards* a bottom the next token has
    // already moved, so its intermediate positions read as "the user scrolled away"
    // and would disarm the follow it is in the middle of performing.
    messagesEndRef.current?.scrollIntoView({ behavior: showStream ? "auto" : "smooth" });
  }, [messages, visibleStreamText, showStream]);

  const refreshSessions = useCallback(() => {
    api.listSessions().then(setSessions).catch(() => {});
  }, []);

  // Builds a stateful event applier for one turn. Shared by a fresh send and by
  // reattaching to a turn found already running after a reload, so both render
  // identically instead of drifting apart.
  const makeEventApplier = useCallback((sessionForMsg: string | null) => {
    let fullText = "";
    const steps: string[] = [];
    let thinking = "";
    let meta: TurnMeta | undefined;

    return (event: StreamEvent) => {
      if (event.type === "queued") {
        setStreamingQueued(event.ahead ?? 1);
        return;
      }
      // Any real progress means a slot was acquired.
      setStreamingQueued(null);
      switch (event.type) {
        case "text":
          fullText = event.text || "";
          setStreamingText(fullText);
          break;
        case "thinking":
          thinking = event.thinking || "";
          setStreamingThinking(thinking);
          break;
        case "tool_use":
          if (event.label) {
            steps.push(event.label);
            setStreamingSteps([...steps]);
          }
          break;
        case "result":
          if (event.meta) {
            meta = event.meta;
            setStreamingMeta(meta);
          }
          break;
        case "complete": {
          // Only render into the transcript if the user is still looking at the
          // session this turn belongs to. Otherwise the answer would be appended to
          // whichever conversation happens to be open. It is already persisted, so
          // it will be there when they open the right one.
          if (sessionForMsg && sessionForMsg === activeSessionIdRef.current) {
            const assistantMsg: Message = {
              id: Date.now() + 1,
              role: "assistant",
              content: fullText,
              steps_json: steps.length > 0 ? steps : undefined,
              thinking: thinking || undefined,
              meta: event.meta || meta,
              trace_id: event.trace_id,
              session_id: sessionForMsg,
              created_at: new Date().toISOString(),
            };
            setMessages((prev) => [...prev, assistantMsg]);
          }
          setStreamingText("");
          setStreamingSteps([]);
          setStreamingThinking("");
          setStreamingMeta(undefined);
          api.listSessions().then(setSessions).catch(() => {});
          // The turn may have generated a report via the tool, so the sidebar list has to
          // catch up — the fence in the answer is not the only place it should appear.
          if (sessionForMsg) {
            api.listReports(sessionForMsg).then(setReports).catch(() => {});
          }
          break;
        }
        case "error":
          toast.error(event.error || "Agent error");
          break;
      }
    };
  }, []);

  const handleSend = useCallback(async () => {
    const msg = input.trim();
    if (!msg || isStreaming || readOnly) return;

    setInput("");
    // Asking a question is a request to watch the answer, wherever they had
    // scrolled to while composing it.
    stickToBottomRef.current = true;
    setIsStreaming(true);
    setStreamingText("");
    setStreamingSteps([]);
    setStreamingThinking("");
    setStreamingMeta(undefined);

    // Optimistic user message
    const userMsg: Message = {
      id: Date.now(),
      role: "user",
      content: msg,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    try {
      const { execution_id, session_id } = await api.invokeAgent(msg, activeSessionId || undefined, genieMode);
      setExecutionId(execution_id);
      ownStreamRef.current = execution_id;
      setStreamingSessionId(activeSessionId || session_id);

      if (!activeSessionId) {
        setActiveSessionId(session_id);
        api.listSessions().then(setSessions).catch(() => {});

      }

      // Stream progress
      const abort = new AbortController();
      abortRef.current = abort;
      await streamProgress(
        execution_id,
        makeEventApplier(activeSessionId || session_id),
        abort.signal
      );
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== "AbortError") {
        toast.error(e.message || "Failed to send message");
      }
    } finally {
      setIsStreaming(false);
      setStreamingSessionId(null);
      setStreamingQueued(null);
      setExecutionId(null);
      ownStreamRef.current = null;
      abortRef.current = null;
    }
  }, [input, isStreaming, activeSessionId, readOnly, makeEventApplier, genieMode]);

  // Reattach to a turn still in flight, or surface what an interrupted one
  // produced. Without this, reloading the page shows an empty conversation while
  // the agent keeps working server-side.
  const reattach = useCallback(async (sid: string) => {
    if (ownStreamRef.current) return;
    let info: ActiveExecution;
    try {
      info = await api.getActiveExecution(sid);
    } catch {
      return; // best-effort: never let this break loading a session
    }
    if (!info.execution_id || ownStreamRef.current) return;

    if (info.status === "running") {
      const apply = makeEventApplier(sid);
      setIsStreaming(true);
      setStreamingSessionId(sid);
      setExecutionId(info.execution_id);
      ownStreamRef.current = info.execution_id; // stop a second reattach doubling up
      (info.events || []).forEach(apply); // catch up on what was missed
      const abort = new AbortController();
      abortRef.current = abort;
      try {
        await streamProgress(
          info.execution_id, apply, abort.signal, info.event_count || 0
        );
      } catch {
        /* ignore — the turn continues server-side regardless */
      } finally {
        setIsStreaming(false);
        setStreamingSessionId(null);
        setStreamingQueued(null);
        setExecutionId(null);
        ownStreamRef.current = null;
        abortRef.current = null;
      }
      return;
    }

    if (info.status === "interrupted") {
      // The events are the only record of this answer — no message row was ever
      // written — so show the partial result rather than silently dropping it.
      const texts = (info.events || []).filter((e) => e.type === "text" && e.text);
      const steps = (info.events || [])
        .filter((e) => e.type === "tool_use" && e.label)
        .map((e) => e.label as string);
      if (texts.length) {
        setMessages((prev) => [...prev, {
          id: -Date.now(),
          role: "assistant",
          content:
            (texts[texts.length - 1].text || "") +
            "\n\n---\n\n⚠️ _This turn was interrupted before it finished (the app " +
            "restarted). The answer above is partial — ask again for a complete one._",
          steps_json: steps.length ? steps : undefined,
          created_at: null,
        }]);
      }
    }
  }, [makeEventApplier]);

  // Load messages when the session changes, then reattach to any turn already
  // running for it. Declared after reattach/makeEventApplier so it can use them.
  useEffect(() => {
    if (!activeSessionId) {
      setMessages([]);
      setReadOnly(false);
      setSessionOwner(null);
      setReports([]);
      return;
    }
    // Feedback state belongs to one conversation; carrying it across would let a
    // rating typed for one session be submitted against another.
    setSessionRating(null);
    setSessionComment("");
    setSessionFeedbackSent(false);
    const sid = activeSessionId;
    api.getSession(sid)
      .then((data) => {
        setMessages(data.messages);
        // The server decides who may write; the UI only reflects that verdict.
        // Sending into another user's session would advance their cli_session_id.
        setReadOnly(Boolean(data.read_only));
        setSessionOwner(data.owner_email ?? null);

		api.listReports(sid).then(setReports).catch(() => setReports([]));		api.getGenieMode(sid).then((r) => {
          if (r.mode) setGenieMode(r.mode);
        }).catch(() => {});        void reattach(sid);
      })
      .catch(() => toast.error("Failed to load session"));
  }, [activeSessionId, reattach]);

  const handleStop = useCallback(() => {
    if (executionId) {
      api.stopStream(executionId).catch(() => {});
    }
    abortRef.current?.abort();
  }, [executionId]);

  const handleNewSession = useCallback(() => {
    setActiveSessionId(null);
    setMessages([]);
    setStreamingText("");
    setGenieMode("agent");
    setSessionRating(null);
    setSessionComment("");
    setSessionFeedbackSent(false);
    setReadOnly(false);
    setSessionOwner(null);
    setReports([]);
    inputRef.current?.focus();
  }, []);

  /** The button path. Same generator as asking in the chat — this is discoverability:
   *  nobody guesses they can ask for a PDF. */
  const handleGenerateReport = useCallback(async () => {
    if (!activeSessionId || reportBusy) return;
    setReportBusy(true);
    try {
      const created = await api.createReport(activeSessionId);
      setReports((prev) => [created, ...prev]);
      window.open(`/api/reports/${created.id}`, "_blank", "noopener");
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not generate the report");
    } finally {
      setReportBusy(false);
    }
  }, [activeSessionId, reportBusy]);

  const startRename = useCallback((s: SessionSummary) => {
    setRenamingId(s.id);
    setRenameDraft(s.title || "");
  }, []);

  /** Save the draft title. Closes the editor first, so the save is optimistic and the
   *  list never sits in a half-edited state waiting on the network. */
  const commitRename = useCallback(async (id: string) => {
    const next = renameDraft.trim().replace(/\s+/g, " ");
    const previous = sessions.find((s) => s.id === id)?.title || "";
    setRenamingId(null);
    // An empty box means "never mind" rather than "erase the name", and an unchanged
    // title means there is nothing to send. Both also make a save idempotent, which
    // matters because Enter and the blur it causes can both land here.
    if (!next || next === previous) return;

    setSessions((cur) => cur.map((s) => (s.id === id ? { ...s, title: next } : s)));
    try {
      const res = await api.updateSessionTitle(id, next);
      // The server trims and caps; show what it stored, not what was typed.
      if (res.title && res.title !== next) {
        setSessions((cur) => cur.map((s) => (s.id === id ? { ...s, title: res.title! } : s)));
      }
    } catch (e: unknown) {
      // Revert this row only. Replacing the whole array would clobber a session list
      // refresh that landed while the request was in flight.
      setSessions((cur) => cur.map((s) => (s.id === id ? { ...s, title: previous } : s)));
      toast.error(e instanceof Error ? e.message : "Could not rename the session");
    }
  }, [renameDraft, sessions]);

  const handleDeleteSession = useCallback(async (id: string) => {
    await api.deleteSession(id).catch(() => toast.error("Failed to delete"));
    if (activeSessionId === id) {
      setActiveSessionId(null);
      setMessages([]);
    }
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, [activeSessionId]);

  const submitSessionFeedback = useCallback(async () => {
    if (!activeSessionId) return;
    if (!sessionRating && !sessionComment.trim()) return;
    try {
      // No trace_id sent on purpose: the server resolves the session's *first* trace.
      // This used to send the latest answer's trace, which MLflow reads as a judgement
      // on that single reply rather than on the conversation.
      await api.submitFeedback({
        session_id: activeSessionId,
        level: "session",
        rating: sessionRating ? String(sessionRating) : "",
        comment: sessionComment.trim() || undefined,
      });
      setSessionFeedbackSent(true);
      toast.success("Session rating submitted — thank you");
    } catch {
      toast.error("Failed to submit rating");
    }
  }, [activeSessionId, sessionRating, sessionComment]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  /** On POSE le mode demandé au lieu de basculer : le contrôle segmenté montre
   *  les deux options, l'utilisateur en choisit une. L'ancien bouton affichait
   *  l'état courant avec « click to switch », ce qui laisse toujours planer le
   *  doute — le libellé désigne-t-il l'état actuel ou la cible ? */
  const handleSetGenieMode = useCallback((next: GenieMode) => {
    if (next === genieMode) return;
    api.setGenieMode(next, activeSessionId || undefined).then((r) => setGenieMode(r.mode)).catch(() => {});
  }, [genieMode, activeSessionId]);

  // Composer placeholder: one statement, not a nested ternary (Sonar S3358).
  let composerPlaceholder = "Ask Turbine Lens anything about your fleet...";
  if (readOnly) composerPlaceholder = "Read-only — viewing another user's session";
  else if (otherSessionStreaming) composerPlaceholder = "A turn is still running in another session…";

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside
        style={{ width: sidebarWidth }}
        className="bg-surface border-r border-gray-200 flex flex-col shrink-0"
      >
        {/* Logo + Identity */}
        <div className="p-4 border-b border-gray-100">
          <h1 className="mb-2">
            <ProductLockup product="Renewable Fleet Analyst" size="lg" />
            <span className="sr-only">Turbine Lens Renewable Fleet Analyst</span>
          </h1>
          {user && (
            <p className="text-xs text-gray-600 mt-2">
              You are <span className="font-semibold">{user.email}</span>
            </p>
          )}
        </div>

        {/* Contrôle de mode — placé ICI, et pas dans le pied avec le thème,
            parce qu'il n'a pas la même portée : `setGenieMode` l'applique à la
            SESSION ouverte, c'est donc un réglage de la conversation, pas de
            l'application. Sa place est au-dessus de la conversation qu'il
            gouverne. Le thème, lui, vaut pour toute l'app et reste en pied. */}
        <div className="px-4 pb-2">
          <span className="block text-xs text-gray-500 mb-1">Genie</span>
          <UiSegmentedControl
            value={genieMode}
            onChange={(valeur) => handleSetGenieMode(valeur as GenieMode)}
            scale="md"
            aria-label="Mode d'appel de Genie"
          >
            <UiSegmentedControlButton value="agent">Agent Mode</UiSegmentedControlButton>
            <UiSegmentedControlButton value="classic">Classic</UiSegmentedControlButton>
          </UiSegmentedControl>
        </div>

        {/* New Chat button */}
        <div className="p-3">
          {/* UiButton prend `label` et `icon` (nom d'icône Material) : lui passer
              des ENFANTS le ferait sortir vide, sans erreur. L'icône vient donc
              des familles Material auto-hébergées, plus de lucide ici. */}
          <UiButton
            className="w-full"
            onClick={handleNewSession}
            label="New chat"
            icon="add"
            variant="primary"
            emphasis="bold"
          />
        </div>

        {/* Search */}
        <div className="px-3 pb-2">
          {/* PIÈGE : la valeur arrive en DEUXIÈME argument de `onChange`. Lire
              `e.target.value` marcherait à la frappe mais casserait sur le bouton
              d'effacement, qui est un <button> et n'a donc pas de `value` — la
              recherche resterait alors bloquée sur le dernier terme saisi.
              Le composant apporte aussi son icône et l'aria-label du bouton
              d'effacement, d'où la disparition du gabarit maison. */}
          <UiInputSearch
            value={searchQuery}
            /* `value` n'est pas typé `string` mais `string | number |
               readonly string[] | undefined` — il hérite du type `value` d'un
               <input>. La conversion est donc obligatoire, et TypeScript le
               signale (contrairement au piège du 2e argument, qui, lui, ne se
               voit qu'à l'exécution). */
            onChange={(_e, value) => setSearchQuery(String(value ?? ""))}
            placeholder="Search sessions…"
            /* Le nom accessible doit être posé sur l'INPUT, via `inputRef`.
               `UiInputSearchProps` fusionne les props natives sur le <div>
               englobant, donc un `aria-label` passé normalement n'atteint jamais
               le champ : mesuré, l'input n'avait ni `aria-label` ni
               `aria-labelledby`, et tirait son nom du <label> interne — dont le
               texte est l'icône, « search ». Un lecteur d'écran annonçait donc
               « search » au lieu de « Search sessions ». Aucune prop du composant
               ne permet de le corriger autrement. */
            inputRef={(el: HTMLInputElement | null) => el?.setAttribute("aria-label", "Search sessions")}
            scale="sm"
          />
        </div>

        {/* Sessions header + Mine/All filter */}
        <div className="px-4 pb-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-500 uppercase">Sessions</span>
            {/* UiSegmentedControl plutôt que UiRadioGroup, arbitré au banc.
                Les deux expriment un choix exclusif et la charte admet les radios
                pour 2 à 5 options, mais UiRadioGroup rend sa légende VISIBLE et en
                gras, et son échelle minimale est `md` : la rangée compacte
                « SESSIONS · filtre » était cassée en hauteur. Le contrôle segmenté
                tient sur la ligne, et il est déjà employé deux fois plus haut dans
                cette même barre — la cohérence joue aussi. */}
            <UiSegmentedControl
              value={scopeFilter}
              onChange={(valeur) => setScopeFilter(valeur === "mine" ? "mine" : "all")}
              scale="sm"
              aria-label="Scope of the session list"
            >
              <UiSegmentedControlButton value="mine">Mine</UiSegmentedControlButton>
              <UiSegmentedControlButton value="all">All</UiSegmentedControlButton>
            </UiSegmentedControl>
          </div>
          {/* UiButton et non UiIconButton : ce contrôle portait un LIBELLÉ
              visible (« Refresh »). Le passer en bouton icône seul le réduisait à
              un glyphe, donc supposait que l'icône se suffise — elle ne se
              suffit pas ici, et c'est la même perte que celle déjà commise sur le
              contrôle de mode Genie. Un bouton icône ne convient qu'à une action
              dont le pictogramme est univoque (envoyer, supprimer). */}
          <UiButton
            onClick={refreshSessions}
            label="Refresh"
            icon="refresh"
            variant="primary"
            emphasis="minimal"
            scale="xs"
            title="Refresh sessions"
          />
        </div>

        {/* Session list */}
        <div className="flex-1 overflow-y-auto px-2 space-y-1">
          {/* Ligne de session : un conteneur NON interactif qui juxtapose un vrai <button>
              (ouvrir la conversation — clavier natif, plus de role="button" sur un div,
              Sonar S6819) et les actions du propriétaire (renommer, supprimer) À CÔTÉ, pas
              dedans : imbriquer deux contrôles est invalide. Pendant un renommage, le bouton
              cède la place au champ de saisie pour la même raison. */}
          {filteredSessions.map((s) => {
            const active = activeSessionId === s.id;
            const renaming = renamingId === s.id;
            const dot = (
              <div className={cn("w-2.5 h-2.5 rounded-full mt-1.5 shrink-0", active ? "bg-turbine_lens-primary" : "bg-gray-300")} />
            );
            const subtitle = (
              <span className="block text-xs text-gray-400 mt-0.5">
                {s.owner_email?.split("@")[0]} · {timeAgo(s.updated_at)} · {s.message_count} msg
              </span>
            );
            return (
              <div
                key={s.id}
                className={cn(
                  "group relative flex items-start gap-2 px-3 py-2.5 rounded-button transition border",
                  active ? "bg-turbine_lens-primary/5 border-turbine_lens-primary/30" : "border-transparent hover:bg-gray-50"
                )}
              >
                {renaming ? (
                  <div className="flex-1 min-w-0 flex items-start gap-2">
                    {dot}
                    <div className="flex-1 min-w-0">
                      <input
                        autoFocus
                        value={renameDraft}
                        maxLength={SESSION_TITLE_MAX}
                        onChange={(e) => setRenameDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); commitRename(s.id); }
                          if (e.key === "Escape") { e.preventDefault(); setRenamingId(null); }
                        }}
                        onBlur={() => commitRename(s.id)}
                        className="w-full text-sm font-medium text-gray-800 bg-surface border border-turbine_lens-primary/50 rounded px-1.5 py-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-turbine_lens-primary"
                      />
                      {subtitle}
                    </div>
                  </div>
                ) : (
                  <button
                    type="button"
                    aria-current={active}
                    onClick={() => setActiveSessionId(s.id)}
                    className="flex-1 min-w-0 flex items-start gap-2 text-left rounded-button cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-turbine_lens-primary"
                  >
                    {dot}
                    <div className="flex-1 min-w-0">
                      {/* La charte ne définit que deux graisses (400, 700) : la session
                          ACTIVE est en 700, là où la distinction sert. */}
                      <span className={cn("block text-sm text-gray-800 truncate", active ? "font-bold" : "font-normal")}>
                        {s.title || "Untitled"}
                      </span>
                      {subtitle}
                    </div>
                  </button>
                )}
                {/* Only the owner may rename or delete; the API enforces this too. Both are
                    hidden while the title is being edited — a click on either would blur the
                    input and race its own save. */}
                {s.owner_email === user?.email && !renaming && (
                  <>
                    <UiIconButton
                      onClick={() => startRename(s)}
                      icon="edit"
                      variant="secondary"
                      scale="2xs"
                      title="Rename session"
                      aria-label="Rename session"
                      className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition"
                    />
                    <UiIconButton
                      onClick={() => handleDeleteSession(s.id)}
                      icon="delete"
                      variant="destructive"
                      scale="2xs"
                      title="Delete session"
                      aria-label="Delete session"
                      className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition"
                    />
                  </>
                )}
              </div>
            );
          })}
          {filteredSessions.length === 0 && (
            <p className="text-center text-xs text-gray-400 py-4">No sessions found</p>
          )}
        </div>

        {/* Session report. The chat prompt is the primary trigger — this button exists so
            the feature is discoverable at all, and calls the same server-side generator.
          Readers can generate snapshots without writing into the conversation. */}
        {activeSessionId && (
          <div className="p-3 border-t border-gray-100">
            <p className="text-xs text-gray-400 mb-1.5">Session report</p>
            <UiButton
              className="w-full"
              onClick={handleGenerateReport}
              disabled={reportBusy || messages.length === 0}
              label={reportBusy ? "Generating…" : "Generate report"}
              icon="description"
              variant="primary"
              emphasis="subtle"
              scale="sm"
              title="Generate a printable report of this session"
            />
            {reports.length > 0 && (
              <ul className="mt-2 space-y-1">
                {reports.map((r) => (
                  <li key={r.id}>
                    <a
                      href={`/api/reports/${r.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center justify-between text-xs text-gray-500 hover:text-turbine_lens-primary transition"
                    >
                      <span>v{r.version}</span>
                      <span className="text-gray-400">
                        {r.created_at ? timeAgo(r.created_at) : ""}
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Session rating (bottom of sidebar). Stars only *select*; submitting is
            explicit, so a comment can be written before anything is sent — the Gradio
            UI worked this way and firing on the star click silently dropped comments. */}
        {activeSessionId && (
          <div className="p-3 border-t border-gray-100">
            {sessionFeedbackSent ? (
              <p className="text-xs text-gray-400">⭐ Thanks — session feedback recorded.</p>
            ) : (
              <>
                <p className="text-xs text-gray-400 mb-1">Rate this session:</p>
                <div className="flex gap-1">
                  {[1, 2, 3, 4, 5].map((n) => (
                    <button type="button"
                      key={n}
                      onClick={() => setSessionRating(n)}
                      title={`${n} of 5`}
                      className={cn(
                        "p-1 rounded transition",
                        sessionRating && n <= sessionRating
                          ? "text-yellow-500"
                          : "text-gray-300 hover:text-yellow-400"
                      )}
                    >
                      <Star size={16} fill={sessionRating && n <= sessionRating ? "currentColor" : "none"} />
                    </button>
                  ))}
                </div>
                {/* `isMultiline` rend un <textarea> plutôt qu'un <input>. Le
                    libellé est masqué visuellement mais présent pour les lecteurs
                    d'écran : le bloc porte déjà son titre juste au-dessus, le
                    répéter le ferait annoncer deux fois. */}
                <UiFormItem
                  id="session-comment"
                  name="session-comment"
                  isMultiline
                  rows={2}
                  label="Comments on the whole session"
                  labelKind="static"
                  placeholder="Comments on the whole session (optional)"
                  value={sessionComment}
                  onChange={(e) => setSessionComment((e.target as HTMLTextAreaElement).value)}
                  className="mt-2 [&_label]:sr-only"
                />
                <UiButton
                  className="mt-2 w-full"
                  onClick={submitSessionFeedback}
                  disabled={!sessionRating && !sessionComment.trim()}
                  label="Submit session rating"
                  variant="primary"
                  emphasis="bold"
                  scale="sm"
                />
              </>
            )}
          </div>
        )}

        {/* Pied de la barre latérale — DERNIER élément, et ancré en bas par
            `mt-auto`.
            Il était auparavant placé AVANT les blocs « rapport » et « notation »,
            donc il n'était un pied que tant qu'aucune session n'était ouverte :
            dès que ces blocs apparaissaient, ils se plaçaient en dessous et le
            poussaient vers le haut. C'est ce que Julien voyait « remonter » quand
            « Rate this session » s'affichait. Deux corrections, pas une : la
            position dans le DOM, et `mt-auto` pour qu'il reste collé en bas même
            quand le contenu au-dessus est court. */}
        <div className="mt-auto px-3 py-3 flex items-center justify-between gap-2 border-t border-gray-100">
          <a
            href="#/cost"
            className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-turbine_lens-primary transition"
            title="Estimate monthly cost at a given number of users"
          >
            <Calculator size={13} />
            Cost forecast
          </a>
          <ThemeToggle />
        </div>
      </aside>
      <SidebarResizer width={sidebarWidth} onResize={setSidebarWidth} />

      {/* Main chat area */}
      {/* `min-w-0` n'est pas décoratif : un enfant flex a `min-width: auto` par
          défaut, donc il REFUSE de rétrécir sous la largeur intrinsèque de son
          contenu. Un tableau large dans une réponse poussait ainsi toute la mise
          en page au-delà de la fenêtre — mesuré à 981px de débord sur une fenêtre
          de 900px — et le navigateur ajoutait une barre horizontale sur la page
          entière, en décalant la barre latérale hors de l'écran. */}
      <main className="flex-1 min-w-0 flex flex-col bg-surface">
        {/* Persistence is configured but not working — say so, because otherwise
            the app looks healthy while nothing is being saved. */}
        {config && config.persistence_configured && !config.persistence_enabled && (
          <div className="border-b border-red-200 bg-red-50 px-4 py-2 text-center text-xs text-red-800">
            ⚠️ Persistence is degraded — this conversation is being kept in memory
            only and <span className="font-semibold">will be lost when the app restarts</span>.
          </div>
        )}

        {/* Messages */}
        <div
          ref={scrollAreaRef}
          onScroll={handleScrollAreaScroll}
          className="flex-1 overflow-y-auto px-6 py-4"
        >
          {messages.length === 0 && !showStream && (
            <div className="flex items-center justify-center h-full text-gray-400">
              <div className="text-center">
                <img src="/turbine_lens_mark.svg" alt="Turbine Lens" className="w-16 h-16 mx-auto mb-4 opacity-30" />
                <p className="text-lg font-medium text-gray-600">How can I help you today?</p>
                <p className="text-sm mt-1">Ask about your wind fleet, energy production, or anomalies.</p>
              </div>
            </div>
          )}

          {messages.map((m) => (
            <ChatMessage key={m.id} message={m} sessionId={activeSessionId} />
          ))}

          {/* Streaming preview — only for the session the turn actually belongs to */}
          {showStream && (
            <ChatMessage
              message={{
                id: -1,
                role: "assistant",
                content: streamingText,
                steps_json: streamingSteps.length > 0 ? streamingSteps : undefined,
                thinking: streamingThinking || undefined,
                meta: streamingMeta,
                created_at: null,
              }}
              isStreaming
              queuedAhead={streamingQueued}
              sessionId={activeSessionId}
            />
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Read-only replay banner — viewing a colleague's session */}
        {readOnly && (
          <div className="border-t border-amber-200 bg-amber-50 px-4 py-2 text-center text-xs text-amber-800">
            🔒 Read-only replay — this session belongs to{" "}
            <span className="font-semibold">{sessionOwner}</span>
          </div>
        )}

        {/* Input area */}
        <div className="border-t bg-gray-50 p-4">
          <div className="max-w-4xl mx-auto flex items-end gap-2">
            {/* ÉCRITE À LA MAIN, délibérément. UiFormItem sait rendre un
                <textarea> (`isMultiline`) et les deux zones de COMMENTAIRE de
                l'app y sont passées — mais c'est un champ de formulaire étiqueté,
                pas une zone de composition. Ici il ajouterait un libellé et une
                empreinte plus grande, et mettrait en jeu deux comportements
                fonctionnels : le `ref` qui donne le focus après création d'une
                session, et la touche Entrée qui envoie. Le gain serait nul, le
                risque réel. Même arbitrage que pour les étoiles de notation et les
                dépliants d'une réponse. */}
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={readOnly || otherSessionStreaming}
              placeholder={composerPlaceholder}
              rows={1}
              aria-describedby="composer-hint"
              className={cn(
                "flex-1 resize-none rounded-button border border-gray-300 px-4 py-3 text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-turbine_lens-primary focus:border-turbine_lens-primary",
                (readOnly || otherSessionStreaming) && "bg-gray-100 text-gray-400 cursor-not-allowed"
              )}
            />
            {showStream ? (
              <UiIconButton
                onClick={handleStop}
                icon="stop"
                variant="destructive"
                scale="lg"
                title="Stop generation"
                aria-label="Stop generation"
              />
            ) : (
              <UiIconButton
                onClick={handleSend}
                disabled={!input.trim() || readOnly}
                icon="send"
                variant="brand"
                scale="lg"
                title="Send message"
                aria-label="Send message"
              />
            )}
          </div>
          <p id="composer-hint" className="max-w-4xl mx-auto mt-1 text-xs text-gray-500">
            Enter to send · Shift+Enter for a new line
          </p>
        </div>
      </main>
    </div>
  );
}
