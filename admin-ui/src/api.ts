export type DeviceStatus = 'pending' | 'approved' | 'revoked'

export interface DeviceRecord {
  device_id: string
  name: string
  profile_name: string
  token_preview: string
  status: DeviceStatus
  firmware_version: string
  board_model: string
  created_at: string | null
  approved_at: string | null
  last_seen_at: string | null
  online: boolean
}

export interface DeviceSummary {
  total: number
  online: number
  pending: number
  approved: number
  revoked: number
}

export interface AudioProfile {
  sample_rate: number
  frame_duration_ms: number
  tts_encode_frame_ms: number
  tts_bitrate: string
  max_speech_sec: number
  vad_check_every_packets: number
  vad_check_min_packets: number
}

export interface VadProfile {
  trailing_silence_ms: number
  min_loud_speech_ms: number
  trailing_rel_level: number
  trailing_min_rms: number
  aggressiveness: number
}

export interface ScreenProfile {
  status_channel: 'stt' | 'tts' | 'both'
  screen_image: boolean
  size: [number, number]
  mcp: boolean
}

export interface ProfileRecord {
  name: string
  audio: AudioProfile
  vad: VadProfile
  screen: ScreenProfile
  updated_at: string | null
  device_count: number
}

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data?.detail) detail = String(data.detail)
    } catch {
      // ignore non-json error body
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  me: () => req<{ authenticated: boolean }>('/api/admin/me'),
  login: (password: string) =>
    req<{ authenticated: boolean }>('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  logout: () => req<{ authenticated: boolean }>('/api/admin/logout', { method: 'POST' }),

  listDevices: () =>
    req<{ devices: DeviceRecord[]; summary: DeviceSummary }>('/api/admin/devices'),
  approveDevice: (deviceId: string, body: { name?: string; profile_name?: string }) =>
    req<DeviceRecord>(`/api/admin/devices/${encodeURIComponent(deviceId)}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  revokeDevice: (deviceId: string) =>
    req<DeviceRecord>(`/api/admin/devices/${encodeURIComponent(deviceId)}/revoke`, {
      method: 'POST',
    }),
  updateDevice: (deviceId: string, body: { name?: string; profile_name?: string }) =>
    req<DeviceRecord>(`/api/admin/devices/${encodeURIComponent(deviceId)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  deleteDevice: (deviceId: string) =>
    req<{ deleted: boolean }>(`/api/admin/devices/${encodeURIComponent(deviceId)}`, {
      method: 'DELETE',
    }),

  listProfiles: () => req<{ profiles: ProfileRecord[] }>('/api/admin/profiles'),
  createProfile: (body: {
    name: string
    audio: AudioProfile
    vad: VadProfile
    screen: ScreenProfile
  }) =>
    req<ProfileRecord>('/api/admin/profiles', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateProfile: (
    name: string,
    body: { audio: AudioProfile; vad: VadProfile; screen: ScreenProfile },
  ) =>
    req<ProfileRecord>(`/api/admin/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteProfile: (name: string) =>
    req<{ deleted: boolean }>(`/api/admin/profiles/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
}

export { ApiError }
