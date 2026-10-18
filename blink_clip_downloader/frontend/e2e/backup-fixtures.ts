import { createHash } from 'node:crypto'
import { crc32 } from 'node:zlib'

// Builds backup files byte by byte, so a test can make a good one, a damaged
// one, or one from a version that does not exist yet. The real writer is
// Python (blink_downloader/backup/); this is only enough ZIP to hand it
// something to read, with every checksum worked out the way it does.

export interface Member {
  name: string
  data: Buffer
}

const DOS_EPOCH_DATE = 0x21 // 1980-01-01, which a ZIP header needs some date for

/** A ZIP holding exactly these members, stored without compression. */
export function zip(members: Member[]): Buffer {
  const parts: Buffer[] = []
  const directory: Buffer[] = []
  let offset = 0
  for (const { name, data } of members) {
    const nameBytes = Buffer.from(name, 'utf8')
    const checksum = crc32(data)

    const local = Buffer.alloc(30)
    local.writeUInt32LE(0x04034b50, 0)
    local.writeUInt16LE(20, 4)
    local.writeUInt16LE(0x0800, 6) // names are UTF-8
    local.writeUInt16LE(DOS_EPOCH_DATE, 12)
    local.writeUInt32LE(checksum, 14)
    local.writeUInt32LE(data.length, 18)
    local.writeUInt32LE(data.length, 22)
    local.writeUInt16LE(nameBytes.length, 26)
    parts.push(local, nameBytes, data)

    const entry = Buffer.alloc(46)
    entry.writeUInt32LE(0x02014b50, 0)
    entry.writeUInt16LE(20, 4)
    entry.writeUInt16LE(20, 6)
    entry.writeUInt16LE(0x0800, 8)
    entry.writeUInt16LE(DOS_EPOCH_DATE, 14)
    entry.writeUInt32LE(checksum, 16)
    entry.writeUInt32LE(data.length, 20)
    entry.writeUInt32LE(data.length, 24)
    entry.writeUInt16LE(nameBytes.length, 28)
    entry.writeUInt32LE(offset, 42)
    directory.push(entry, nameBytes)

    offset += local.length + nameBytes.length + data.length
  }
  const central = Buffer.concat(directory)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50, 0)
  end.writeUInt16LE(members.length, 8)
  end.writeUInt16LE(members.length, 10)
  end.writeUInt32LE(central.length, 12)
  end.writeUInt32LE(offset, 16)
  return Buffer.concat([...parts, central, end])
}

export interface BackupOptions {
  /** Records per table, as the database would hold them. */
  tables: Record<string, object[]>
  formatVersion?: number
  /** Changes a record after its checksum is taken: a backup edited by hand. */
  tamper?: boolean
}

const sha256 = (data: Buffer) => createHash('sha256').update(data).digest('hex')

/** A backup in the format blink_downloader/backup/archive.py reads. */
export function backupZip({ tables, formatVersion = 1, tamper = false }: BackupOptions): Buffer {
  const manifestTables: Record<string, object> = {}
  const members: Member[] = []
  for (const [name, rows] of Object.entries(tables)) {
    const data = Buffer.from(rows.map((row) => JSON.stringify(row) + '\n').join(''), 'utf8')
    manifestTables[name] = { size: data.length, sha256: sha256(data), rows: rows.length }
    members.push({
      name: `database/${name}.jsonl`,
      data: tamper ? Buffer.from(data.toString('utf8').replace(/e2e/g, 'xxx'), 'utf8') : data,
    })
  }
  const manifest = {
    format: 'blink-clip-downloader-backup',
    format_version: formatVersion,
    addon_version: '6.1.3',
    created_at: '2026-10-18T12:00:00+00:00',
    clip_folder: '/share/blink-clips',
    tables: manifestTables,
    files: {},
  }
  return zip([{ name: 'manifest.json', data: Buffer.from(JSON.stringify(manifest)) }, ...members])
}

export const SCRATCH_CLIP = 'e2e-restore-clip'

/** One clip, with an analysis, that no seeded library holds. */
export function scratchClipBackup(options: Partial<BackupOptions> = {}): Buffer {
  return backupZip({
    tables: {
      clips: [
        {
          id: SCRATCH_CLIP,
          camera: 'Restore Scratch Cam',
          file_path: `/share/blink-clips/${SCRATCH_CLIP}.mp4`,
          timestamp: '2026-10-01T09:00:00+00:00',
          downloaded_at: '2026-10-01T09:01:00+00:00',
          starred: true,
          tags: '["restored"]',
        },
      ],
      analysis_results: [
        {
          id: 1,
          clip_id: SCRATCH_CLIP,
          camera: 'Restore Scratch Cam',
          model: 'e2e-model',
          summary: 'Someone left a parcel',
          analyzed_at: '2026-10-01T09:02:00+00:00',
        },
      ],
    },
    ...options,
  })
}
