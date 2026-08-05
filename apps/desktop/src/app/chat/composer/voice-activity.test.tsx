import { afterEach, describe, expect, it, vi } from 'vitest'

import { getElementAnalyser } from './voice-activity'

describe('getElementAnalyser', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('does not attach a media element source while the audio context is suspended', () => {
    const createMediaElementSource = vi.fn(() => ({ connect: vi.fn() }))

    const createAnalyser = vi.fn(() => ({
      connect: vi.fn(),
      fftSize: 0,
      frequencyBinCount: 1,
      getByteFrequencyData: vi.fn(),
      smoothingTimeConstant: 0
    }))

    const resume = vi.fn(() => Promise.resolve())

    class SuspendedAudioContext {
      createAnalyser = createAnalyser
      createMediaElementSource = createMediaElementSource
      destination = {}
      resume = resume
      state = 'suspended'
    }

    vi.stubGlobal('AudioContext', SuspendedAudioContext)

    const audio = document.createElement('audio')

    expect(getElementAnalyser(audio)).toBeNull()
    expect(resume).toHaveBeenCalledTimes(1)
    expect(createMediaElementSource).not.toHaveBeenCalled()
  })
})
