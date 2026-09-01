import type { Provider } from '../../api/types'

/**
 * Return the models a provider can offer in the composer.
 *
 * Some compatible gateways intentionally return an empty `/models` catalog
 * while still accepting a configured model. Keep that explicit model usable
 * without inventing any additional models.
 */
export function providerModelIds(provider: Pick<Provider, 'models' | 'model'> | undefined): string[] {
  const discovered = (provider?.models ?? []).map((id) => String(id).trim()).filter(Boolean)
  if (discovered.length > 0) return discovered
  const configured = String(provider?.model ?? '').trim()
  return configured ? [configured] : []
}
