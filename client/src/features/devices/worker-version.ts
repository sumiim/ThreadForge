import type { Device, WorkerReleaseManifest } from '../../api/types'

type ParsedVersion = readonly [number, number, number]

function parseVersion(value: string | null | undefined): ParsedVersion | null {
  if (!value || !/^\d+\.\d+\.\d+$/.test(value)) return null
  const parts = value.split('.').map(Number)
  if (parts.some((part) => !Number.isSafeInteger(part))) return null
  return [parts[0], parts[1], parts[2]] as ParsedVersion
}

/**
 * Compare two Worker versions using numeric SemVer components.
 *
 * A null result means one of the values is missing or malformed. Callers
 * should treat that as "not safe to use" rather than guessing ordering.
 */
export function compareWorkerVersions(
  left: string | null | undefined,
  right: string | null | undefined,
): number | null {
  const parsedLeft = parseVersion(left)
  const parsedRight = parseVersion(right)
  if (!parsedLeft || !parsedRight) return null
  for (let index = 0; index < parsedLeft.length; index += 1) {
    if (parsedLeft[index] > parsedRight[index]) return 1
    if (parsedLeft[index] < parsedRight[index]) return -1
  }
  return 0
}

export function workerVersionAtLeast(
  version: string | null | undefined,
  minimum: string | null | undefined,
): boolean {
  const comparison = compareWorkerVersions(version, minimum)
  return comparison !== null && comparison >= 0
}

export function workerNeedsUpdate(
  device: Pick<Device, 'online' | 'compatible' | 'version'>,
  release: Pick<WorkerReleaseManifest, 'version'> | null,
): boolean {
  if (!device.online) return false
  if (!device.compatible || !release) return true
  return !workerVersionAtLeast(device.version, release.version)
}

export function workerIsReady(
  device: Pick<Device, 'online' | 'compatible' | 'version'>,
  release: Pick<WorkerReleaseManifest, 'version'> | null,
): boolean {
  return device.online && device.compatible && workerVersionAtLeast(device.version, release?.version)
}
