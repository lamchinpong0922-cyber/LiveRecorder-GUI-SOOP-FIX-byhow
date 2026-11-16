
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,

  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react"

import {
  CircleHelp,
  ExternalLink,
  FolderOpen,
  Moon,
  Pencil,
  Play,
  Plus,
  Radio,
  Search,
  Settings2,
  Square,
  Sun,
  Trash2,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { useTheme } from "@/components/theme-provider"

import {
  api,
  emptyRoom,
  emptySettings,
  roomLabel,
  type LogLine,
  type Room,
  type RoomDraft,
  type SettingsDraft,
  type Snapshot,
  type ShutdownState,
} from "@/api"

import {
  RoomEditor,
  SettingsEditor,
} from "@/EditorDialogs"

import {
  LanguageToggle,
  useLanguage,
} from "@/i18n"


type LogResponse = {
  logs: LogLine[]
  running: boolean
}


export default function RecorderApp() {
  const { theme, setTheme } = useTheme()
  const { language, t } = useLanguage()

  const [state, setState] = useState<Snapshot | null>(null)

  const [selected, setSelected] = useState(0)
  const [roomQuery, setRoomQuery] = useState("")

  const [logs, setLogs] = useState<LogLine[]>([])
  const [logFilter, setLogFilter] =
    useState<"all" | "error" | "success">("all")

  const [autoScroll, setAutoScroll] = useState(true)
  const [notice, setNotice] = useState("")
  const [roomsSyncTime, setRoomsSyncTime] = useState("--:--:--")
  const [roomsSyncStatus, setRoomsSyncStatus] = useState("waiting")
  const [stopPending, setStopPending] = useState(false)
  const [stopFailures, setStopFailures] = useState<Array<{path: string; error: string}>>([])

  // 三欄布局寬度：保存使用者上次調整的結果。
  const layoutContainer = useRef<HTMLDivElement>(null)

  const [layoutWidths, setLayoutWidths] = useState(() => {
    const defaults = { sidebar: 280, detail: 350 }

    try {
      const saved = JSON.parse(
        localStorage.getItem("live-recorder-layout") || "null"
      )

      return {
        sidebar: Number.isFinite(saved?.sidebar)
          ? Math.max(220, Math.min(600, saved.sidebar))
          : defaults.sidebar,
        detail: Number.isFinite(saved?.detail)
          ? Math.max(280, Math.min(700, saved.detail))
          : defaults.detail,
      }
    } catch {
      return defaults
    }
  })

  const resizeDrag = useRef<{
    column: "sidebar" | "detail"
    startX: number
    startSidebar: number
    startDetail: number
    containerWidth: number
  } | null>(null)

  useEffect(() => {
    localStorage.setItem(
      "live-recorder-layout",
      JSON.stringify(layoutWidths)
    )
  }, [layoutWidths])

  function startColumnResize(
    event: ReactPointerEvent<HTMLDivElement>,
    column: "sidebar" | "detail",
  ) {
    if (event.button !== 0) return

    const rect = layoutContainer.current?.getBoundingClientRect()
    if (!rect) return

    event.preventDefault()

    resizeDrag.current = {
      column,
      startX: event.clientX,
      startSidebar: layoutWidths.sidebar,
      startDetail: layoutWidths.detail,
      containerWidth: rect.width,
    }

    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function moveColumnResize(
    event: ReactPointerEvent<HTMLDivElement>,
  ) {
    const drag = resizeDrag.current
    if (!drag) return

    const delta = event.clientX - drag.startX

    if (drag.column === "sidebar") {
      const maximum = Math.max(
        220,
        drag.containerWidth - drag.startDetail - 280 - 14
      )

      setLayoutWidths((current) => ({
        ...current,
        sidebar: Math.round(
          Math.max(220, Math.min(maximum, drag.startSidebar + delta))
        ),
      }))
    } else {
      const maximum = Math.max(
        280,
        drag.containerWidth - drag.startSidebar - 280 - 14
      )

      setLayoutWidths((current) => ({
        ...current,
        detail: Math.round(
          Math.max(280, Math.min(maximum, drag.startDetail + delta))
        ),
      }))
    }
  }

  function stopColumnResize() {
    resizeDrag.current = null
  }

  function resetLayoutWidths() {
    resizeDrag.current = null
    setLayoutWidths({ sidebar: 280, detail: 350 })
  }
  const [shutdownMode, setShutdownMode] =
    useState<"once" | "daily">("once")

  const [shutdownTime, setShutdownTime] =
    useState("03:00")

  const [shutdownState, setShutdownState] =
    useState<ShutdownState | null>(null)

  const [roomOpen, setRoomOpen] = useState(false)

  useEffect(() => {
    roomOpenRef.current = roomOpen

    if (!roomOpen && pendingExternalRooms.current) {
      const latestRooms = pendingExternalRooms.current
      pendingExternalRooms.current = null

      setState((current) =>
        current ? { ...current, rooms: latestRooms } : current
      )
    }
  }, [roomOpen])
  const [editing, setEditing] = useState<number | null>(null)
  const [roomDraft, setRoomDraft] =
    useState<RoomDraft>(emptyRoom)

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsDraft, setSettingsDraft] =
    useState<SettingsDraft>(emptySettings)

  const [deleteOpen, setDeleteOpen] = useState(false)

  const [previewTick, setPreviewTick] = useState(0)
  const [previewFailed, setPreviewFailed] = useState(false)

  const logSince = useRef(0)
  const roomsRevision = useRef("")
  const roomOpenRef = useRef(false)
  const pendingExternalRooms = useRef<Room[] | null>(null)
  const editingRoomUrl = useRef<string | null>(null)
  const logEnd = useRef<HTMLDivElement>(null)

  const shutdownInitialized = useRef(false)

  const rooms = state?.rooms ?? []
  const selectedRoom = rooms[selected] ?? null

  const label = (room: Room) =>
    roomLabel(room, t("unnamedRoom"))

  const isRoomRecording = (room: Room) =>
    (state?.recordingRoomUrls ?? []).includes(room.url)

  const enabledCount =
    rooms.filter((room) => room.enabled).length

  const normalizedQuery =
    roomQuery.trim().toLocaleLowerCase()

  const visibleRooms = rooms
    .map((room, index) => ({ room, index }))
    .filter(({ room }) =>
      !normalizedQuery ||
      `${label(room)} ${room.url}`
        .toLocaleLowerCase()
        .includes(normalizedQuery)
    )

  const visibleLogs =
    logFilter === "all"
      ? logs
      : logs.filter((line) => line.level === logFilter)

  const previewSupported =
    !!selectedRoom &&
    /play\.sooplive\.(com|co\.kr)\/.+\/\d+/
      .test(selectedRoom.url)

  const shutdownBusy =
    shutdownState?.phase === "stopping" ||
    shutdownState?.phase === "countdown" ||
    shutdownState?.phase === "shutdown_retry"


  function announce(message: string) {
    setNotice(message)

    window.setTimeout(() => {
      setNotice((current) =>
        current === message ? "" : current
      )
    }, 4200)
  }


  async function loadState() {
    const data = await api<Snapshot>("/api/state")

    setState(data)
    setShutdownState(data.shutdown)

    setSelected((current) =>
      Math.max(
        0,
        Math.min(current, data.rooms.length - 1)
      )
    )
  }


  useEffect(() => {
    let active = true

    api<Snapshot>("/api/state")
      .then((data) => {
        if (!active) return

        setState(data)
        setShutdownState(data.shutdown)

        if (!shutdownInitialized.current) {
          setShutdownMode(data.shutdown.mode)
          setShutdownTime(data.shutdown.time)
          shutdownInitialized.current = true
        }
      })
      .catch((error: Error) => {
        announce(error.message)
      })

    const previewTimer = window.setInterval(() => {
      setPreviewFailed(false)
      setPreviewTick(Date.now())
    }, 15000)

    return () => {
      active = false
      window.clearInterval(previewTimer)
    }
  }, [])


  useEffect(() => {
    let active = true
    let timer = 0

    async function refreshState() {
      try {
        const data = await api<Snapshot>("/api/state")

        if (!active) return

        const revision =
          (data as Snapshot & { roomsRevision?: string }).roomsRevision ?? ""

        if (revision && revision !== roomsRevision.current) {
          const wasInitialized = !!roomsRevision.current
          roomsRevision.current = revision

          if (wasInitialized) {
            setRoomsSyncTime(new Date().toLocaleTimeString())
            setRoomsSyncStatus("synced")

            if (roomOpenRef.current) {
              pendingExternalRooms.current = data.rooms
            }
          } else {
            setRoomsSyncTime(new Date().toLocaleTimeString())
            setRoomsSyncStatus("synced")
          }
        }

        setState((current) => {
          if (!current) return data

          return {
            ...data,
            rooms:
              roomOpenRef.current && pendingExternalRooms.current
                ? current.rooms
                : data.rooms,
          }
        })
        setShutdownState(data.shutdown)

        setSelected((current) =>
          Math.max(
            0,
            Math.min(current, data.rooms.length - 1)
          )
        )

      } catch {
        // 下次輪詢重新嘗試。
      }

      if (active) {
        timer = window.setTimeout(refreshState, 1000)
      }
    }

    timer = window.setTimeout(refreshState, 1000)

    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [])


  useEffect(() => {
    let active = true
    let timer = 0

    async function poll() {
      try {
        const data = await api<LogResponse>(
          `/api/logs?since=${logSince.current}`
        )

        if (!active) return

        if (data.logs.length) {
          logSince.current = Math.max(
            logSince.current,
            ...data.logs.map((line) => line.id)
          )

          setLogs((current) =>
            [...current, ...data.logs].slice(-700)
          )
        }

        setState((current) =>
          current
            ? { ...current, running: data.running }
            : current
        )

      } catch {
        // 下次輪詢重新嘗試。
      }

      if (active) {
        timer = window.setTimeout(poll, 1200)
      }
    }

    poll()

    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [])


  useEffect(() => {
    if (autoScroll) {
      logEnd.current?.scrollIntoView({
        block: "end",
      })
    }
  }, [logs, autoScroll])


  async function controlRoom(
    index: number,
    enabled: boolean,
  ) {
    try {
      const result = await api<{
        rooms: Room[]
        running: boolean
        message: string
      }>("/api/room/control", {
        index,
        enabled,
      })

      setState((current) =>
        current
          ? {
              ...current,
              rooms: result.rooms,
              running: result.running,
            }
          : current
      )

      announce(
        language === "en"
          ? t(
              enabled
                ? result.running
                  ? "roomStarted"
                  : "coreStartFailed"
                : "roomStopped"
            )
          : result.message
      )

    } catch (error) {
      announce((error as Error).message)
    }
  }


  function openRoom(index: number | null) {
    editingRoomUrl.current =
      index === null ? null : rooms[index]?.url ?? null

    roomOpenRef.current = true
    setEditing(index)

    setRoomDraft(
      index === null
        ? { ...emptyRoom }
        : { ...rooms[index] }
    )

    setRoomOpen(true)
  }


  async function saveRoom(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    if (!/^https?:\/\//i.test(roomDraft.url.trim())) {
      announce(t("invalidUrl"))
      return
    }

    const baseRooms = (
      pendingExternalRooms.current ?? rooms
    ).map((room) => ({ ...room }))

    const updated = baseRooms

    const item = {
      ...roomDraft,
      url: roomDraft.url.trim(),
      name: roomDraft.name.trim(),
    }

    let target = updated.length

    if (editing === null) {
      updated.push({
        ...item,
        id: String(target),
      })
    } else {
      const originalUrl = editingRoomUrl.current
      const latestIndex = pendingExternalRooms.current && originalUrl
        ? updated.findIndex((room) => room.url === originalUrl)
        : editing

      if (latestIndex >= 0 && latestIndex < updated.length) {
        target = latestIndex
        updated[latestIndex] = {
          ...updated[latestIndex],
          ...item,
        }
      } else {
        target = updated.length
        updated.push({ ...item, id: String(target) })
      }
    }

    try {
      const result = await api<{
        rooms: Room[]
      }>("/api/rooms/save", {
        rooms: updated,
      })

      setState((current) =>
        current
          ? { ...current, rooms: result.rooms }
          : current
      )

      setSelected(target)
      setRoomQuery("")
      setPreviewFailed(false)
      pendingExternalRooms.current = null
      roomOpenRef.current = false
      roomsRevision.current = ""
      setRoomsSyncTime(new Date().toLocaleTimeString())
      setRoomsSyncStatus("synced")
      setRoomOpen(false)

      if (item.enabled && !state?.running) {
        await api("/api/recorder/start", {})
      }

      if (
        !result.rooms.some((room) => room.enabled) &&
        state?.running
      ) {
        await api("/api/recorder/stop", {})
      }

      await loadState()
      announce(t("roomSaved"))

    } catch (error) {
      announce((error as Error).message)
    }
  }


  async function deleteRoom() {
    if (!selectedRoom) return

    try {
      const latestRooms = pendingExternalRooms.current ?? rooms
      const deletingUrl = selectedRoom.url

      const result = await api<{
        rooms: Room[]
      }>("/api/rooms/save", {
        rooms: latestRooms.filter(
          (room) => room.url !== deletingUrl
        ),
      })

      pendingExternalRooms.current = null
      roomsRevision.current = ""
      setRoomsSyncTime(new Date().toLocaleTimeString())
      setRoomsSyncStatus("synced")

      setState((current) =>
        current
          ? { ...current, rooms: result.rooms }
          : current
      )

      setSelected(Math.max(0, selected - 1))
      setPreviewFailed(false)
      setDeleteOpen(false)

      if (
        !result.rooms.some((room) => room.enabled) &&
        state?.running
      ) {
        await api("/api/recorder/stop", {})
      }

      await loadState()
      announce(t("roomDeleted"))

    } catch (error) {
      announce((error as Error).message)
    }
  }


  function openSettings() {
    if (!state) return

    setSettingsDraft({
      ...state.settings,
      soopPassword: "",
      soopCookie: "",
    })

    setSettingsOpen(true)
  }


  async function saveSettings(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    try {
      await api("/api/settings", settingsDraft)

      setSettingsOpen(false)

      await loadState()
      announce(t("settingsSaved"))

    } catch (error) {
      announce((error as Error).message)
    }
  }


  async function openDownloads() {
    try {
      await api("/api/open-downloads", {})
    } catch (error) {
      announce((error as Error).message)
    }
  }


  async function stopAllRecordings() {
    if (!state?.running || stopPending) return

    try {
      setStopPending(true)
      setStopFailures([])

      const result = await api<{
        ok: boolean
        message: string
        items?: Array<{path: string; success: boolean; error?: string}>
        failed?: Array<{path: string; error: string}>
      }>("/api/recorder/stop", {})

      if (!result.ok) {
        const failures = result.failed ?? []
        setStopFailures(
          failures.length
            ? failures
            : [{path: "停止或轉檔程序", error: result.message}]
        )
        setStopPending(false)
        announce(result.message)
        return
      }

      announce(result.message)

      await new Promise(resolve => window.setTimeout(resolve, 2000))

      const desktopApi = (
        window as Window & {
          pywebview?: {
            api?: { exit_app?: () => Promise<unknown> }
          }
        }
      ).pywebview?.api

      if (!desktopApi?.exit_app) {
        setStopPending(false)
        setStopFailures([{
          path: "桌面程式關閉",
          error: "無法連接桌面關閉介面，請從系統匣選擇「停止錄製並結束程式」"
        }])
        return
      }

      await desktopApi.exit_app()

    } catch (error) {
      setStopPending(false)
      setStopFailures([{
        path: "停止全部並轉 MP4",
        error: (error as Error).message
      }])
      announce((error as Error).message)
    }
  }


  async function saveShutdownSchedule() {
    try {
      const result = await api<{
        ok: boolean
        shutdown: ShutdownState
      }>("/api/shutdown/schedule", {
        mode: shutdownMode,
        time: shutdownTime,
      })

      setShutdownState(result.shutdown)

      announce(
        language === "en"
          ? "Shutdown schedule saved"
          : "定時關機已設定"
      )

    } catch (error) {
      announce((error as Error).message)
    }
  }


  async function cancelShutdownSchedule() {
    try {
      const result = await api<{
        ok: boolean
        shutdown: ShutdownState
      }>("/api/shutdown/cancel", {})

      setShutdownState(result.shutdown)

      announce(
        language === "en"
          ? "Shutdown cancelled"
          : "定時關機已取消"
      )

    } catch (error) {
      announce((error as Error).message)
    }
  }


  return (
    <div
      ref={layoutContainer}
      className="app-shell"
      style={{
        "--sidebar-width": `${layoutWidths.sidebar}px`,
        "--detail-width": `${layoutWidths.detail}px`,
      } as CSSProperties}
    >

      <aside className="room-sidebar">

        <div className="brand-block">
          <Radio
            className="brand-mark"
            aria-hidden="true"
          />
          <div className="brand-title">
            LIVE RECORDER
          </div>
        </div>

        <div className="sidebar-summary">
          <span
            className={`status-dot ${
              state?.running ? "is-active" : ""
            }`}
          />
          <span>
            {t(
              state?.running
                ? "coreRunning"
                : "coreStopped"
            )}
          </span>
          <span className="ml-auto tabular-nums">
            {t("enabledCount", {
              enabled: enabledCount,
              total: rooms.length,
            })}
          </span>
        </div>

        <div className="sidebar-actions">
          <Button
            variant="outline"
            onClick={openSettings}
          >
            <Settings2 />
            {t("settings")}
          </Button>

          <Button onClick={() => openRoom(null)}>
            <Plus />
            {t("addRoom")}
          </Button>
        </div>

        <div className="sidebar-section-title">
          <span>{t("rooms")}</span>
          <span className="text-xs text-muted-foreground">
            {roomsSyncStatus === "synced"
              ? language === "en"
                ? `Synced ${roomsSyncTime}`
                : `已同步 ${roomsSyncTime}`
              : language === "en"
                ? "Waiting for sync"
                : "等待同步"}
          </span>
          <span>
            {visibleRooms.length} / {rooms.length}
          </span>
        </div>

        <div className="room-search">
          <Search aria-hidden="true" />

          <Input
            type="search"
            value={roomQuery}
            onChange={(event) =>
              setRoomQuery(event.target.value)
            }
            placeholder={t("searchRooms")}
            aria-label={t("searchRooms")}
          />
        </div>

        <ScrollArea className="room-scroll">
          <div className="room-items">

            {rooms.length === 0 && (
              <div className="empty-list">
                {t("noRooms")}
                <br />
                {t("addRoomHint")}
              </div>
            )}

            {rooms.length > 0 &&
              visibleRooms.length === 0 && (
                <div className="empty-list">
                  {t("noSearchResults", {
                    query: roomQuery.trim(),
                  })}
                </div>
              )}

            {visibleRooms.map(({ room, index }) => (
              <div
                className={`room-item ${
                  selected === index ? "selected" : ""
                }`}
                key={`${room.url}-${index}`}
              >
                <button
                  className="room-select"
                  onClick={() => {
                    setSelected(index)
                    setPreviewFailed(false)
                  }}
                  aria-label={t("selectRoomNamed", {
                    name: label(room),
                  })}
                  aria-current={
                    selected === index ? "true" : undefined
                  }
                >
                  <span
                    className={`status-dot ${
                      room.enabled ? "is-active" : ""
                    }`}
                  />

                  <span className="room-text">
                    <strong>{label(room)}</strong>
                    <small>
                      {room.quality} {" "}
                      {isRoomRecording(room) ? (
                        <span style={{ color: "#3b82f6", fontWeight: 600 }}>
                          {language === "en" ? "Recording" : "正在錄製中"}
                        </span>
                      ) : (
                        t(
                          room.enabled
                            ? "monitoringEnabled"
                            : "paused"
                        )
                      )}
                    </small>
                  </span>
                </button>

                <Button
                  size="xs"
                  variant="outline"
                  className={
                    room.enabled ? "room-stop" : ""
                  }
                  onClick={() =>
                    controlRoom(index, !room.enabled)
                  }
                  aria-label={t("roomAction", {
                    action: t(
                      room.enabled ? "stop" : "start"
                    ),
                    name: label(room),
                  })}
                >
                  {room.enabled ? (
                    <Square className="size-3" />
                  ) : (
                    <Play className="size-3" />
                  )}

                  {t(room.enabled ? "stop" : "start")}
                </Button>
              </div>
            ))}

          </div>
        </ScrollArea>

        <div className="sidebar-footer">
          <span>LIVE RECORDER  v0.4.3</span>

          <div className="sidebar-preferences">
            <LanguageToggle />

            <Button
              variant="ghost"
              size="icon"
              onClick={() =>
                setTheme(
                  theme === "dark" ? "light" : "dark"
                )
              }
              aria-label={t(
                theme === "dark"
                  ? "lightTheme"
                  : "darkTheme"
              )}
              title={t(
                theme === "dark"
                  ? "lightTheme"
                  : "darkTheme"
              )}
            >
              {theme === "dark" ? <Sun /> : <Moon />}
            </Button>
          </div>
        </div>

      </aside>


            <div
        className="column-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="調整直播列表寬度"
        title="拖曳調整寬度，雙擊恢復預設"
        onPointerDown={(event) =>
          startColumnResize(event, "sidebar")
        }
        onPointerMove={moveColumnResize}
        onPointerUp={stopColumnResize}
        onPointerCancel={stopColumnResize}
        onLostPointerCapture={stopColumnResize}
        onDoubleClick={resetLayoutWidths}
      />
<main className="detail-panel">

        <div className="panel-eyebrow">
          {t("roomDetails")}
        </div>

        <div className="preview-frame">
          {previewSupported && !previewFailed ? (
            <img
              src={`/api/preview?url=${encodeURIComponent(
                selectedRoom!.url
              )}&t=${previewTick}`}
              alt={t("previewAlt", {
                name: label(selectedRoom!),
              })}
              onError={() => setPreviewFailed(true)}
            />
          ) : (
            <div className="preview-placeholder">
              <Radio className="size-7" />
              <span>
                {t(
                  !selectedRoom
                    ? "selectForPreview"
                    : previewSupported
                      ? "previewUnavailable"
                      : "previewUnsupported"
                )}
              </span>
            </div>
          )}
        </div>

        <div className="detail-heading">
          <h1>
            {selectedRoom
              ? label(selectedRoom)
              : t("selectRoom")}
          </h1>

          <span className="detail-state">
            <span
              className={`status-dot ${
                selectedRoom?.enabled ? "is-active" : ""
              }`}
            />

            {selectedRoom && isRoomRecording(selectedRoom) ? (
              <span style={{ color: "#3b82f6", fontWeight: 600 }}>
                {language === "en" ? "Recording" : "正在錄製中"}
              </span>
            ) : (
              t(
                selectedRoom
                  ? selectedRoom.enabled
                    ? "monitoringEnabled"
                    : "paused"
                  : "notSelected"
              )
            )}
          </span>
        </div>

        <Separator />

        <dl className="detail-fields">

          <div className="detail-field">
            <dt>{t("roomUrl")}</dt>
            <dd>
              {selectedRoom ? (
                <a
                  href={selectedRoom.url}
                  target="_blank"
                  rel="noreferrer"
                  className="room-url"
                >
                  {selectedRoom.url}
                  <ExternalLink className="size-3.5" />
                </a>
              ) : (
                <span className="field-empty">—</span>
              )}
            </dd>
          </div>

          <div className="detail-field">
            <dt>{t("targetQuality")}</dt>
            <dd>{selectedRoom?.quality ?? "—"}</dd>
          </div>

          <div className="detail-field">
            <dt>{t("storageFormat")}</dt>
            <dd>
              {state?.settings.format === "mp3音頻"
                ? t("mp3Audio")
                : state?.settings.format === "m4a音頻"
                  ? t("m4aAudio")
                  : state?.settings.format ?? "—"}
            </dd>
          </div>

        </dl>


        <section className="shutdown-panel">

          <h2>{t("shutdownTitle")}</h2>

          <div className="shutdown-controls">

            <select
              className="native-select"
              value={shutdownMode}
              onChange={(event) =>
                setShutdownMode(
                  event.target.value as "once" | "daily"
                )
              }
              disabled={shutdownBusy}
            >
              <option value="once">
                {t("shutdownOnce")}
              </option>

              <option value="daily">
                {t("shutdownDaily")}
              </option>
            </select>

            <input
              className="native-select"
              type="time"
              value={shutdownTime}
              onChange={(event) =>
                setShutdownTime(event.target.value)
              }
              disabled={shutdownBusy}
            />

            <Button
              onClick={saveShutdownSchedule}
              disabled={shutdownBusy}
            >
              {t("shutdownSave")}
            </Button>

            <Button
              variant="destructive"
              onClick={cancelShutdownSchedule}
              disabled={
                !shutdownState?.enabled &&
                !shutdownBusy
              }
            >
              {t("shutdownCancel")}
            </Button>

          </div>

          <div className="shutdown-status">
            {shutdownState?.message ||
              t("shutdownInactive")}
          </div>

          {shutdownState?.nextRun && (
            <div className="shutdown-status">
              {t("shutdownNextRun")}:{" "}
              {shutdownState.nextRun.replace("T", " ")}
            </div>
          )}

          {shutdownState?.phase === "countdown" && (
            <div className="shutdown-status">
              {t("shutdownCountdown")}:{" "}
              {Math.floor(
                shutdownState.remaining / 60
              )}
              :
              {String(
                shutdownState.remaining % 60
              ).padStart(2, "0")}
            </div>
          )}

        </section>


        <div className="detail-note">
          <CircleHelp className="size-4" />
          <span>{t("detailNote")}</span>
        </div>

        <div className="detail-actions">

          <Button
            disabled={!selectedRoom}
            variant={
              selectedRoom?.enabled
                ? "outline"
                : "default"
            }
            onClick={() =>
              selectedRoom &&
              controlRoom(
                selected,
                !selectedRoom.enabled
              )
            }
          >
            {selectedRoom?.enabled ? (
              <Square />
            ) : (
              <Play />
            )}

            {t(
              selectedRoom?.enabled
                ? "stopThisRoom"
                : "startThisRoom"
            )}
          </Button>

          <Button
            disabled={!selectedRoom}
            variant="outline"
            onClick={() => openRoom(selected)}
          >
            <Pencil />
            {t("edit")}
          </Button>

          <Button
            disabled={!selectedRoom}
            variant="outline"
            className="delete-button"
            onClick={() => setDeleteOpen(true)}
          >
            <Trash2 />
            {t("delete")}
          </Button>

        </div>

      </main>


            <div
        className="column-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="調整直播詳情寬度"
        title="拖曳調整寬度，雙擊恢復預設"
        onPointerDown={(event) =>
          startColumnResize(event, "detail")
        }
        onPointerMove={moveColumnResize}
        onPointerUp={stopColumnResize}
        onPointerCancel={stopColumnResize}
        onLostPointerCapture={stopColumnResize}
        onDoubleClick={resetLayoutWidths}
      />
<section className="log-panel">

        <div className="log-header">
          <h2>{t("logs")}</h2>

          <div className="flex items-center gap-2">

            <Button
              variant="destructive"
              disabled={!state?.running || stopPending}
              onClick={stopAllRecordings}
            >
              <Square />
              {stopPending
                ? language === "en"
                  ? "Stopping and finishing conversion..."
                  : "正在停止錄製並完成轉檔..."
                : language === "en"
                  ? "Exit and finish conversion"
                  : "退出並完成轉檔"}
            </Button>

            <Button
              variant="outline"
              onClick={openDownloads}
            >
              <FolderOpen />
              {t("recordingsFolder")}
            </Button>

          </div>
        </div>


        <div className="log-summary">

          <span
            className={`status-dot ${
              state?.running ? "is-active" : ""
            }`}
          />

          <span>
            {t(
              state?.running
                ? "coreRunningShort"
                : "coreStoppedShort"
            )}
          </span>

          <label className="log-filter-label">
            {t("show")}

            <select
              className="native-select log-filter"
              value={logFilter}
              onChange={(event) =>
                setLogFilter(
                  event.target.value as
                    | "all"
                    | "error"
                    | "success"
                )
              }
            >
              <option value="all">{t("all")}</option>
              <option value="error">{t("errors")}</option>
              <option value="success">
                {t("successes")}
              </option>
            </select>
          </label>

          <span className="log-count">
            {t("logCount", {
              visible: visibleLogs.length,
              total: logs.length,
            })}
          </span>

        </div>


        <div className="log-window">

          {logs.length === 0 && (
            <div className="log-empty">
              {t("noLogs")}
            </div>
          )}

          {logs.length > 0 &&
            visibleLogs.length === 0 && (
              <div className="log-empty">
                {t("noFilteredLogs")}
              </div>
            )}

          {visibleLogs.map((line) => (
            <div
              className={`log-line level-${line.level}`}
              key={line.id}
            >
              <time>{line.time}</time>
              <span>{line.text}</span>
            </div>
          ))}

          <div ref={logEnd} />

        </div>


        <div className="log-footer">

          <label className="flex items-center gap-2">
            <Checkbox
              checked={autoScroll}
              onCheckedChange={(value) =>
                setAutoScroll(value === true)
              }
            />
            {t("autoScroll")}
          </label>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLogs([])}
          >
            {t("clearDisplay")}
          </Button>

        </div>

      </section>


      <RoomEditor
        open={roomOpen}
        edit={editing !== null}
        draft={roomDraft}
        setDraft={setRoomDraft}
        save={saveRoom}
        close={() => setRoomOpen(false)}
      />

      <SettingsEditor
        open={settingsOpen}
        draft={settingsDraft}
        setDraft={setSettingsDraft}
        current={state?.settings}
        save={saveSettings}
        close={() => setSettingsOpen(false)}
      />

      <Dialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      >
        <DialogContent>

          <DialogHeader>
            <DialogTitle>
              {t("deleteConfirm")}
            </DialogTitle>

            <DialogDescription>
              {t("deleteDescription", {
                name: selectedRoom
                  ? label(selectedRoom)
                  : t("thisRoom"),
              })}
            </DialogDescription>
          </DialogHeader>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteOpen(false)}
            >
              {t("cancel")}
            </Button>

            <Button
              variant="destructive"
              onClick={deleteRoom}
            >
              {t("deleteRoom")}
            </Button>
          </DialogFooter>

        </DialogContent>
      </Dialog>


      {stopFailures.length > 0 && (
        <div className="notice" role="alert">
          <strong>MP4 轉檔失敗，程式保持開啟</strong>
          <ul>
            {stopFailures.map((item, index) => (
              <li key={`${item.path}-${index}`}>
                <div>{item.path}</div>
                <div>{item.error}</div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}

    </div>
  )
}
