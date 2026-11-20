import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import {
  api,
  ApiError,
  AudioProfile,
  DeviceRecord,
  DeviceSummary,
  ProfileRecord,
  ScreenProfile,
  VadProfile,
} from './api'

type Tab = 'devices' | 'profiles'
type Filter = 'all' | 'pending' | 'approved' | 'revoked' | 'online'

const DEFAULT_AUDIO: AudioProfile = {
  sample_rate: 16000,
  frame_duration_ms: 60,
  tts_encode_frame_ms: 20,
  tts_bitrate: '16k',
  max_speech_sec: 10,
  vad_check_every_packets: 3,
  vad_check_min_packets: 10,
}

const DEFAULT_VAD: VadProfile = {
  trailing_silence_ms: 500,
  min_loud_speech_ms: 400,
  trailing_rel_level: 0.35,
  trailing_min_rms: 150,
  aggressiveness: 3,
}

const DEFAULT_SCREEN: ScreenProfile = {
  status_channel: 'both',
  screen_image: false,
  size: [240, 240],
  mcp: true,
}

function fmtTime(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('th-TH', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export default function App() {
  const [authState, setAuthState] = useState<'checking' | 'login' | 'ready'>('checking')
  const [password, setPassword] = useState('')
  const [loginError, setLoginError] = useState('')
  const [tab, setTab] = useState<Tab>('devices')
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(true)

  const [devices, setDevices] = useState<DeviceRecord[]>([])
  const [summary, setSummary] = useState<DeviceSummary>({
    total: 0,
    online: 0,
    pending: 0,
    approved: 0,
    revoked: 0,
  })
  const [profiles, setProfiles] = useState<ProfileRecord[]>([])
  const [drafts, setDrafts] = useState<Record<string, { name: string; profile_name: string }>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; text: string } | null>(null)

  // Profile modal state
  const [editingProfile, setEditingProfile] = useState<{
    isNew: boolean
    name: string
    audio: AudioProfile
    vad: VadProfile
    screen: ScreenProfile
  } | null>(null)

  const loadAll = useCallback(async () => {
    try {
      const [devRes, profRes] = await Promise.all([api.listDevices(), api.listProfiles()])
      setDevices(devRes.devices)
      setSummary(devRes.summary)
      setProfiles(profRes.profiles)
      setDrafts((prev) => {
        const next = { ...prev }
        for (const d of devRes.devices) {
          if (!next[d.device_id]) {
            next[d.device_id] = { name: d.name, profile_name: d.profile_name }
          }
        }
        return next
      })
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthState('login')
        return
      }
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Failed to load data' })
    }
  }, [])

  useEffect(() => {
    api
      .me()
      .then(() => {
        setAuthState('ready')
        loadAll()
      })
      .catch(() => setAuthState('login'))
  }, [loadAll])

  useEffect(() => {
    if (authState !== 'ready' || !autoRefresh) return
    const timer = window.setInterval(loadAll, 5000)
    return () => window.clearInterval(timer)
  }, [authState, autoRefresh, loadAll])

  async function handleLogin(e: FormEvent) {
    e.preventDefault()
    setLoginError('')
    try {
      await api.login(password)
      setPassword('')
      setAuthState('ready')
      await loadAll()
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  async function handleLogout() {
    await api.logout().catch(() => {})
    setAuthState('login')
  }

  function updateDraft(deviceId: string, patch: Partial<{ name: string; profile_name: string }>) {
    setDrafts((prev) => ({
      ...prev,
      [deviceId]: {
        name: prev[deviceId]?.name ?? '',
        profile_name: prev[deviceId]?.profile_name ?? 'default',
        ...patch,
      },
    }))
  }

  async function handleApprove(d: DeviceRecord) {
    setBusyId(d.device_id)
    setNotice(null)
    const draft = drafts[d.device_id] || { name: d.name, profile_name: d.profile_name }
    try {
      await api.approveDevice(d.device_id, {
        name: draft.name,
        profile_name: draft.profile_name,
      })
      setNotice({ kind: 'info', text: `Approved ${draft.name} (${d.device_id})` })
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Approve failed' })
    } finally {
      setBusyId(null)
    }
  }

  async function handleRevoke(d: DeviceRecord) {
    setBusyId(d.device_id)
    setNotice(null)
    try {
      await api.revokeDevice(d.device_id)
      setNotice({ kind: 'info', text: `Revoked ${d.name} (active connection disconnected)` })
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Revoke failed' })
    } finally {
      setBusyId(null)
    }
  }

  async function handleSaveDevice(d: DeviceRecord) {
    setBusyId(d.device_id)
    setNotice(null)
    const draft = drafts[d.device_id] || { name: d.name, profile_name: d.profile_name }
    try {
      await api.updateDevice(d.device_id, {
        name: draft.name,
        profile_name: draft.profile_name,
      })
      setNotice({
        kind: 'info',
        text:
          draft.profile_name !== d.profile_name
            ? `Updated ${draft.name} to profile "${draft.profile_name}" (reconnecting board)`
            : `Saved ${draft.name}`,
      })
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Save failed' })
    } finally {
      setBusyId(null)
    }
  }

  async function handleDeleteDevice(d: DeviceRecord) {
    if (!window.confirm(`Delete board "${d.name}" (${d.device_id})?`)) return
    setBusyId(d.device_id)
    setNotice(null)
    try {
      await api.deleteDevice(d.device_id)
      setNotice({ kind: 'info', text: `Deleted ${d.device_id}` })
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Delete failed' })
    } finally {
      setBusyId(null)
    }
  }

  async function handleSaveProfile(e: FormEvent) {
    e.preventDefault()
    if (!editingProfile) return
    setNotice(null)
    try {
      if (editingProfile.isNew) {
        await api.createProfile({
          name: editingProfile.name,
          audio: editingProfile.audio,
          vad: editingProfile.vad,
          screen: editingProfile.screen,
        })
        setNotice({ kind: 'info', text: `Created profile "${editingProfile.name}"` })
      } else {
        await api.updateProfile(editingProfile.name, {
          audio: editingProfile.audio,
          vad: editingProfile.vad,
          screen: editingProfile.screen,
        })
        setNotice({
          kind: 'info',
          text: `Updated profile "${editingProfile.name}" (active boards on this profile reconnecting)`,
        })
      }
      setEditingProfile(null)
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Profile save failed' })
    }
  }

  async function handleDeleteProfile(p: ProfileRecord) {
    if (!window.confirm(`Delete profile "${p.name}"?`)) return
    setNotice(null)
    try {
      await api.deleteProfile(p.name)
      setNotice({ kind: 'info', text: `Deleted profile "${p.name}"` })
      await loadAll()
    } catch (err) {
      setNotice({ kind: 'error', text: err instanceof Error ? err.message : 'Delete profile failed' })
    }
  }

  const filteredDevices = useMemo(() => {
    const q = search.trim().toLowerCase()
    return devices.filter((d) => {
      if (filter === 'online' && !d.online) return false
      if (filter !== 'all' && filter !== 'online' && d.status !== filter) return false
      if (q && !d.device_id.toLowerCase().includes(q) && !d.name.toLowerCase().includes(q)) {
        return false
      }
      return true
    })
  }, [devices, filter, search])

  if (authState === 'checking') {
    return <div className="login-wrap muted">Loading...</div>
  }

  if (authState === 'login') {
    return (
      <div className="login-wrap">
        <form className="login-card" onSubmit={handleLogin}>
          <h1>XiaoZhi Fleet Admin</h1>
          <p>เข้าสู่ระบบด้วยรหัสผ่านสำหรับทีม (ADMIN_PASSWORD)</p>
          {loginError && <div className="alert error">{loginError}</div>}
          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="pw">Admin Password</label>
            <input
              id="pw"
              type="password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoFocus
              required
            />
          </div>
          <button type="submit" className="btn btn-primary" style={{ width: '100%' }}>
            Sign In
          </button>
        </form>
      </div>
    )
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <h1>XiaoZhi Fleet Admin</h1>
          <div className="topbar-sub">
            จัดการบอร์ด ESP32 (Approve / Revoke) และโปรไฟล์ฮาร์ดแวร์แต่ละรุ่น
          </div>
        </div>
        <div className="topbar-actions">
          <label className="muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Auto 5s
          </label>
          <button className="btn" onClick={loadAll}>
            Refresh
          </button>
          <button className="btn" onClick={handleLogout}>
            Logout
          </button>
        </div>
      </header>

      <section className="stats">
        <div className="stat-card">
          <div className="stat-label">Online Now</div>
          <div className="stat-value green">{summary.online}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Pending Approval</div>
          <div className="stat-value amber">{summary.pending}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Approved</div>
          <div className="stat-value">{summary.approved}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Revoked</div>
          <div className="stat-value red">{summary.revoked}</div>
        </div>
      </section>

      {notice && <div className={`alert ${notice.kind}`}>{notice.text}</div>}

      <nav className="tabs">
        <button className={`tab ${tab === 'devices' ? 'active' : ''}`} onClick={() => setTab('devices')}>
          Devices ({summary.total})
          {summary.pending > 0 && <span className="badge-count">{summary.pending}</span>}
        </button>
        <button className={`tab ${tab === 'profiles' ? 'active' : ''}`} onClick={() => setTab('profiles')}>
          Board Profiles ({profiles.length})
        </button>
      </nav>

      {tab === 'devices' && (
        <>
          <div className="toolbar">
            <div className="pills">
              {(['all', 'pending', 'approved', 'revoked', 'online'] as Filter[]).map((f) => (
                <button
                  key={f}
                  className={`pill ${filter === f ? 'active' : ''}`}
                  onClick={() => setFilter(f)}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>
            <input
              className="input"
              placeholder="ค้นหา MAC หรือชื่อบอร์ด..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ minWidth: 230 }}
            />
          </div>

          <div className="table-wrap">
            {filteredDevices.length === 0 ? (
              <div className="empty">
                ยังไม่มีบอร์ดในรายการนี้ — เมื่อบอร์ดใหม่บูตและเรียก <code>/ota</code> จะมาโผล่ที่แท็บ{' '}
                <strong>PENDING</strong> อัตโนมัติ
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Name</th>
                    <th>MAC (Device-Id)</th>
                    <th>Profile</th>
                    <th>Model / FW</th>
                    <th>Last Seen</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDevices.map((d) => {
                    const draft = drafts[d.device_id] || { name: d.name, profile_name: d.profile_name }
                    const dirty = draft.name !== d.name || draft.profile_name !== d.profile_name
                    const isBusy = busyId === d.device_id
                    return (
                      <tr key={d.device_id}>
                        <td>
                          <div>
                            <span className={`dot ${d.online ? 'online' : 'offline'}`} />
                            <span className={`status-badge ${d.status}`}>{d.status}</span>
                          </div>
                        </td>
                        <td>
                          <input
                            className="input"
                            value={draft.name}
                            onChange={(e) => updateDraft(d.device_id, { name: e.target.value })}
                            style={{ width: 140 }}
                          />
                        </td>
                        <td>
                          <div className="mono">{d.device_id}</div>
                          <div className="muted mono">tok: {d.token_preview}</div>
                        </td>
                        <td>
                          <select
                            className="select"
                            value={draft.profile_name}
                            onChange={(e) => updateDraft(d.device_id, { profile_name: e.target.value })}
                          >
                            {profiles.map((p) => (
                              <option key={p.name} value={p.name}>
                                {p.name}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td>
                          <div>{d.board_model || '-'}</div>
                          <div className="muted">{d.firmware_version || '-'}</div>
                        </td>
                        <td className="muted">{fmtTime(d.last_seen_at)}</td>
                        <td>
                          <div className="row-actions">
                            {d.status !== 'approved' && (
                              <button
                                className="btn btn-green btn-sm"
                                disabled={isBusy}
                                onClick={() => handleApprove(d)}
                              >
                                {d.status === 'pending' ? 'Approve' : 'Re-approve'}
                              </button>
                            )}
                            {d.status === 'approved' && dirty && (
                              <button
                                className="btn btn-primary btn-sm"
                                disabled={isBusy}
                                onClick={() => handleSaveDevice(d)}
                              >
                                Save
                              </button>
                            )}
                            {d.status !== 'revoked' && (
                              <button
                                className="btn btn-danger btn-sm"
                                disabled={isBusy}
                                onClick={() => handleRevoke(d)}
                              >
                                Revoke
                              </button>
                            )}
                            {d.status === 'revoked' && (
                              <button
                                className="btn btn-danger btn-sm"
                                disabled={isBusy}
                                onClick={() => handleDeleteDevice(d)}
                              >
                                Delete
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {tab === 'profiles' && (
        <>
          <div className="toolbar">
            <div className="muted">
              ชุดตั้งค่าไมค์ (VAD) และหน้าจอของแต่ละรุ่นบอร์ด — การแก้โปรไฟล์จะ reconnect บอร์ดรุ่นนั้นอัตโนมัติ
            </div>
            <button
              className="btn btn-primary"
              onClick={() =>
                setEditingProfile({
                  isNew: true,
                  name: '',
                  audio: { ...DEFAULT_AUDIO },
                  vad: { ...DEFAULT_VAD },
                  screen: { ...DEFAULT_SCREEN, size: [240, 240] },
                })
              }
            >
              + New Profile
            </button>
          </div>

          <div className="profiles-grid">
            {profiles.map((p) => (
              <div className="profile-card" key={p.name}>
                <div className="profile-head">
                  <h3>{p.name}</h3>
                  <span className="muted">{p.device_count} board(s)</span>
                </div>
                <div className="kv-grid">
                  <div>
                    <span className="kv-label">VAD silence:</span> {p.vad.trailing_silence_ms} ms
                  </div>
                  <div>
                    <span className="kv-label">Min speech:</span> {p.vad.min_loud_speech_ms} ms
                  </div>
                  <div>
                    <span className="kv-label">Rel level:</span> {p.vad.trailing_rel_level}
                  </div>
                  <div>
                    <span className="kv-label">Aggressiveness:</span> {p.vad.aggressiveness}
                  </div>
                  <div>
                    <span className="kv-label">Channel:</span> {p.screen.status_channel}
                  </div>
                  <div>
                    <span className="kv-label">Screen PNG:</span>{' '}
                    {p.screen.screen_image ? `ON (${p.screen.size[0]}×${p.screen.size[1]})` : 'OFF'}
                  </div>
                  <div>
                    <span className="kv-label">MCP:</span> {p.screen.mcp ? 'ON' : 'OFF'}
                  </div>
                  <div>
                    <span className="kv-label">Audio:</span> {p.audio.sample_rate}Hz / {p.audio.frame_duration_ms}ms
                  </div>
                </div>
                <div className="row-actions" style={{ marginTop: 4 }}>
                  <button
                    className="btn btn-sm"
                    onClick={() =>
                      setEditingProfile({
                        isNew: false,
                        name: p.name,
                        audio: { ...p.audio },
                        vad: { ...p.vad },
                        screen: { ...p.screen, size: [p.screen.size[0], p.screen.size[1]] },
                      })
                    }
                  >
                    Edit
                  </button>
                  {p.name !== 'default' && (
                    <button
                      className="btn btn-danger btn-sm"
                      disabled={p.device_count > 0}
                      title={p.device_count > 0 ? 'Unassign boards before deleting' : ''}
                      onClick={() => handleDeleteProfile(p)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {editingProfile && (
        <div className="modal-backdrop" onClick={() => setEditingProfile(null)}>
          <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={handleSaveProfile}>
            <h2>{editingProfile.isNew ? 'Create Board Profile' : `Edit Profile: ${editingProfile.name}`}</h2>

            {editingProfile.isNew && (
              <div className="field" style={{ marginBottom: 12 }}>
                <label>Profile Name (lowercase, e.g. esp32_s3_box)</label>
                <input
                  className="input"
                  value={editingProfile.name}
                  onChange={(e) => setEditingProfile({ ...editingProfile, name: e.target.value })}
                  required
                />
              </div>
            )}

            <div className="form-section">
              <h4>Microphone & VAD (จับเสียงหยุดพูด)</h4>
              <div className="form-grid">
                <div className="field">
                  <label>Trailing Silence (ms)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.vad.trailing_silence_ms}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        vad: { ...editingProfile.vad, trailing_silence_ms: Number(e.target.value) },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>Min Loud Speech (ms)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.vad.min_loud_speech_ms}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        vad: { ...editingProfile.vad, min_loud_speech_ms: Number(e.target.value) },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>Relative Level (0.1 - 0.6)</label>
                  <input
                    type="number"
                    step="0.05"
                    className="input"
                    value={editingProfile.vad.trailing_rel_level}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        vad: { ...editingProfile.vad, trailing_rel_level: Number(e.target.value) },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>VAD Aggressiveness (0-3)</label>
                  <input
                    type="number"
                    min={0}
                    max={3}
                    className="input"
                    value={editingProfile.vad.aggressiveness}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        vad: { ...editingProfile.vad, aggressiveness: Number(e.target.value) },
                      })
                    }
                  />
                </div>
              </div>
            </div>

            <div className="form-section">
              <h4>Screen & MCP</h4>
              <div className="form-grid">
                <div className="field">
                  <label>Status Channel</label>
                  <select
                    className="select"
                    value={editingProfile.screen.status_channel}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        screen: {
                          ...editingProfile.screen,
                          status_channel: e.target.value as ScreenProfile['status_channel'],
                        },
                      })
                    }
                  >
                    <option value="stt">stt (user line)</option>
                    <option value="tts">tts (assistant line)</option>
                    <option value="both">both</option>
                  </select>
                </div>
                <div className="field">
                  <label>Screen Width (px)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.screen.size[0]}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        screen: {
                          ...editingProfile.screen,
                          size: [Number(e.target.value), editingProfile.screen.size[1]],
                        },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>Screen Height (px)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.screen.size[1]}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        screen: {
                          ...editingProfile.screen,
                          size: [editingProfile.screen.size[0], Number(e.target.value)],
                        },
                      })
                    }
                  />
                </div>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={editingProfile.screen.screen_image}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        screen: { ...editingProfile.screen, screen_image: e.target.checked },
                      })
                    }
                  />
                  Push status as PNG (MCP)
                </label>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={editingProfile.screen.mcp}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        screen: { ...editingProfile.screen, mcp: e.target.checked },
                      })
                    }
                  />
                  Enable MCP
                </label>
              </div>
            </div>

            <div className="form-section">
              <h4>Audio</h4>
              <div className="form-grid">
                <div className="field">
                  <label>Sample Rate (Hz)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.audio.sample_rate}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        audio: { ...editingProfile.audio, sample_rate: Number(e.target.value) },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>Frame Duration (ms)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.audio.frame_duration_ms}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        audio: { ...editingProfile.audio, frame_duration_ms: Number(e.target.value) },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label>Max Speech (sec)</label>
                  <input
                    type="number"
                    className="input"
                    value={editingProfile.audio.max_speech_sec}
                    onChange={(e) =>
                      setEditingProfile({
                        ...editingProfile,
                        audio: { ...editingProfile.audio, max_speech_sec: Number(e.target.value) },
                      })
                    }
                  />
                </div>
              </div>
            </div>

            <div className="modal-footer">
              <button type="button" className="btn" onClick={() => setEditingProfile(null)}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary">
                Save Profile
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
