import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react"
import { shaderSource } from "./liquidOrbShader"
import {
  activationDurationMs,
  audioFlowStrengths,
  audioRules,
  ribbonInstanceCount,
  ribbonStyleIndex,
  settleDurationMs,
  stateSeeds,
  type LiquidOrbState,
} from "./liquidOrbPresets"

export type { LiquidOrbState }

export type LiquidOrbAudioBands = {
  low?: number
  mid?: number
  high?: number
  all?: number
}

export type LiquidOrbHandle = {
  setState: (state: LiquidOrbState) => void
  getState: () => LiquidOrbState
  setAudioBands: (bands: LiquidOrbAudioBands) => void
}

export type LiquidOrbProps = {
  /** "idle" (calm) or "thinking" (active/streaming). Default "idle". */
  state?: LiquidOrbState
  /** Square size in CSS pixels. Default 22 — sized for inline button use. */
  size?: number
  className?: string
  style?: React.CSSProperties
  audioBands?: LiquidOrbAudioBands
  /** Called once if WebGPU isn't available or the render fails; the
   *  component falls back to a plain CSS pulse so the button never breaks. */
  onError?: (error: unknown) => void
}

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value))
}

function srgbToLinear(value: number) {
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
}

function linearToSrgb(value: number) {
  return value <= 0.0031308 ? value * 12.92 : 1.055 * value ** (1 / 2.4) - 0.055
}

function mixSrgb(from: number, to: number, progress: number) {
  return linearToSrgb(srgbToLinear(from) + (srgbToLinear(to) - srgbToLinear(from)) * progress)
}

/**
 * Renders the "Liquid Orb" WebGPU shader as a small self-contained widget.
 * Drive it with the `state` prop ("idle" | "thinking") — e.g. pass
 * `state={loading ? "thinking" : "idle"}`. Falls back to a plain CSS pulse
 * dot if WebGPU isn't supported or the render fails, so it's always safe to
 * drop into a button.
 */
const LiquidOrb = forwardRef<LiquidOrbHandle, LiquidOrbProps>(function LiquidOrb(
  { state: stateProp = "idle", size = 22, className, style, audioBands, onError },
  ref,
) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const apiRef = useRef<LiquidOrbHandle | null>(null)
  const [fallback, setFallback] = useState(
    () => typeof navigator === "undefined" || !("gpu" in navigator),
  )

  useImperativeHandle(
    ref,
    () => ({
      setState: (next) => apiRef.current?.setState(next),
      getState: () => apiRef.current?.getState() ?? stateProp,
      setAudioBands: (bands) => apiRef.current?.setAudioBands(bands),
    }),
    [stateProp],
  )

  // Keep the orb's own state in sync with the `state` prop.
  useEffect(() => {
    apiRef.current?.setState(stateProp)
  }, [stateProp])

  useEffect(() => {
    if (audioBands) apiRef.current?.setAudioBands(audioBands)
  }, [audioBands?.low, audioBands?.mid, audioBands?.high, audioBands?.all])

  useEffect(() => {
    if (fallback) return
    const canvas = canvasRef.current
    if (!canvas) return

    let stopped = false
    let animationFrame = 0
    let device: GPUDevice | null = null
    let ribbonTarget: GPUTexture | null = null
    let lastFrameAt: number | null = null
    let motionPhase = 0

    // --- per-instance transition state (was module-level in the prototype) ---
    let curState: LiquidOrbState = stateProp
    let transitionTargetState: LiquidOrbState = curState
    let fromUniforms = new Float32Array(stateSeeds[curState])
    let targetUniforms = new Float32Array(stateSeeds[curState])
    const displayedUniforms = new Float32Array(stateSeeds[curState])
    let transitionStartedAt = 0
    let activeTransitionDuration = 0
    let bands = { low: 0, mid: 0, high: 0, all: 0 }

    function transitionProgress(now: number) {
      if (activeTransitionDuration === 0) return 1
      const raw = Math.min(
        1,
        Math.max(0, (now - transitionStartedAt) / activeTransitionDuration),
      )
      return transitionTargetState === "thinking" ? 1 - (1 - raw) ** 3 : raw * raw * (3 - 2 * raw)
    }

    function sampleTransition(now: number) {
      const progress = transitionProgress(now)
      for (let index = 3; index < displayedUniforms.length; index += 1) {
        const colorComponent = index >= 40 && (index - 40) % 4 < 3
        displayedUniforms[index] = colorComponent
          ? mixSrgb(fromUniforms[index], targetUniforms[index], progress)
          : fromUniforms[index] + (targetUniforms[index] - fromUniforms[index]) * progress
      }
      return displayedUniforms
    }

    function setOrbState(next: LiquidOrbState) {
      if (!Object.prototype.hasOwnProperty.call(stateSeeds, next) || next === curState) return
      const now = performance.now()
      sampleTransition(now)
      fromUniforms = new Float32Array(displayedUniforms)
      targetUniforms = new Float32Array(stateSeeds[next])
      transitionTargetState = next
      transitionStartedAt = now
      activeTransitionDuration = next === "thinking" ? activationDurationMs : settleDurationMs
      curState = next
    }

    function setOrbAudioBands(next: LiquidOrbAudioBands) {
      bands = {
        low: Number.isFinite(next.low) ? clamp01(next.low as number) : bands.low,
        mid: Number.isFinite(next.mid) ? clamp01(next.mid as number) : bands.mid,
        high: Number.isFinite(next.high) ? clamp01(next.high as number) : bands.high,
        all: Number.isFinite(next.all) ? clamp01(next.all as number) : bands.all,
      }
    }

    function applyAudioUniforms(values: Float32Array) {
      const strength = audioFlowStrengths[Math.round(values[15])] ?? 0
      if (!strength) return
      for (const [index, band, additive, proportional, ceiling] of audioRules) {
        const level = bands[band] * strength
        if (!level) continue
        values[index] = Math.min(
          Math.max(ceiling, values[index]),
          values[index] * (1 + proportional * level) + additive * level,
        )
      }
    }

    apiRef.current = {
      setState: setOrbState,
      getState: () => curState,
      setAudioBands: setOrbAudioBands,
    }
    if (audioBands) setOrbAudioBands(audioBands)

    function fail(error: unknown) {
      if (stopped) return
      stopped = true
      cancelAnimationFrame(animationFrame)
      ribbonTarget?.destroy()
      device?.destroy()
      setFallback(true)
      onError?.(error)
      console.error(error)
    }

    async function start() {
      if (!navigator.gpu) throw new Error("WebGPU is not supported in this environment.")
      const adapter = await navigator.gpu.requestAdapter()
      if (!adapter) throw new Error("No compatible WebGPU adapter was found.")
      device = await adapter.requestDevice()
      const context = canvas!.getContext("webgpu")
      if (!context) throw new Error("Unable to create a WebGPU canvas context.")

      const format = navigator.gpu.getPreferredCanvasFormat()
      context.configure({ device, format, alphaMode: "premultiplied" })
      const shader = device.createShaderModule({ code: shaderSource })
      const compilation = await shader.getCompilationInfo()
      const errors = compilation.messages.filter((m) => m.type === "error")
      if (errors.length) {
        throw new Error(
          errors.map((m) => `${m.lineNum}:${m.linePos} ${m.message}`).join("\n"),
        )
      }

      const pipeline = device.createRenderPipeline({
        layout: "auto",
        vertex: { module: shader, entryPoint: "vs_main" },
        fragment: {
          module: shader,
          entryPoint: "fs_main",
          targets: [
            {
              format,
              blend: {
                color: { srcFactor: "one", dstFactor: "one-minus-src-alpha", operation: "add" },
                alpha: { srcFactor: "one", dstFactor: "one-minus-src-alpha", operation: "add" },
              },
            },
          ],
        },
        primitive: { topology: "triangle-list" },
      })
      const ribbonPipeline = device.createRenderPipeline({
        layout: "auto",
        vertex: { module: shader, entryPoint: "ribbon_vs_main" },
        fragment: {
          module: shader,
          entryPoint: "ribbon_fs_main",
          targets: [
            {
              format,
              blend: {
                color: { srcFactor: "one", dstFactor: "one", operation: "add" },
                alpha: { srcFactor: "one", dstFactor: "one-minus-src-alpha", operation: "add" },
              },
            },
          ],
        },
        primitive: { topology: "triangle-list" },
      })
      const ribbonCompositePipeline = device.createRenderPipeline({
        layout: "auto",
        vertex: { module: shader, entryPoint: "vs_main" },
        fragment: {
          module: shader,
          entryPoint: "ribbon_composite_fs_main",
          targets: [
            {
              format,
              blend: {
                color: { srcFactor: "one", dstFactor: "one-minus-src-alpha", operation: "add" },
                alpha: { srcFactor: "one", dstFactor: "one-minus-src-alpha", operation: "add" },
              },
            },
          ],
        },
        primitive: { topology: "triangle-list" },
      })

      const values = new Float32Array(displayedUniforms)
      const uniformBuffer = device.createBuffer({
        size: values.byteLength,
        usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
      })
      const bindGroup = device.createBindGroup({
        layout: pipeline.getBindGroupLayout(0),
        entries: [{ binding: 0, resource: { buffer: uniformBuffer } }],
      })
      const ribbonBindGroup = device.createBindGroup({
        layout: ribbonPipeline.getBindGroupLayout(0),
        entries: [{ binding: 0, resource: { buffer: uniformBuffer } }],
      })
      const ribbonSampler = device.createSampler({
        addressModeU: "clamp-to-edge",
        addressModeV: "clamp-to-edge",
        magFilter: "linear",
        minFilter: "linear",
      })
      let ribbonCompositeBindGroup: GPUBindGroup | null = null

      device.lost.then((info) => {
        fail(new Error(`WebGPU device lost: ${info.message || info.reason}`))
      })
      device.addEventListener("uncapturederror", (event) => {
        // @ts-expect-error — GPUUncapturedErrorEvent isn't in the default DOM lib yet
        event.preventDefault()
        // @ts-expect-error — see above
        fail(new Error(`WebGPU rendering error: ${event.error.message}`))
      })

      function frame(now: number) {
        if (stopped) return
        try {
          const dpr = Math.min(window.devicePixelRatio || 1, 2)
          const width = Math.max(1, Math.floor(canvas!.clientWidth * dpr))
          const height = Math.max(1, Math.floor(canvas!.clientHeight * dpr))
          if (canvas!.width !== width || canvas!.height !== height) {
            canvas!.width = width
            canvas!.height = height
            ribbonTarget?.destroy()
            ribbonTarget = null
            ribbonCompositeBindGroup = null
          }
          values.set(sampleTransition(now))
          const frameDelta =
            lastFrameAt === null ? 0 : Math.min(0.1, Math.max(0, (now - lastFrameAt) / 1000))
          lastFrameAt = now
          applyAudioUniforms(values)
          motionPhase += frameDelta * Math.max(values[3], 0)
          values[0] = width
          values[1] = height
          values[2] = motionPhase / Math.max(values[3], 0.001)
          device!.queue.writeBuffer(uniformBuffer, 0, values)

          const isParticleRibbon = Math.round(values[15]) === ribbonStyleIndex
          const encoder = device!.createCommandEncoder()
          if (isParticleRibbon) {
            if (!ribbonTarget || !ribbonCompositeBindGroup) {
              ribbonTarget = device!.createTexture({
                size: { width, height },
                format,
                usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.TEXTURE_BINDING,
              })
              ribbonCompositeBindGroup = device!.createBindGroup({
                layout: ribbonCompositePipeline.getBindGroupLayout(0),
                entries: [
                  { binding: 0, resource: { buffer: uniformBuffer } },
                  { binding: 1, resource: ribbonTarget.createView() },
                  { binding: 2, resource: ribbonSampler },
                ],
              })
            }
            const particlePass = encoder.beginRenderPass({
              colorAttachments: [
                {
                  view: ribbonTarget.createView(),
                  clearValue: { r: 0, g: 0, b: 0, a: 0 },
                  loadOp: "clear",
                  storeOp: "store",
                },
              ],
            })
            particlePass.setPipeline(ribbonPipeline)
            particlePass.setBindGroup(0, ribbonBindGroup)
            particlePass.draw(6, ribbonInstanceCount)
            particlePass.end()
          }
          const pass = encoder.beginRenderPass({
            colorAttachments: [
              {
                view: context!.getCurrentTexture().createView(),
                clearValue: { r: 0, g: 0, b: 0, a: 0 },
                loadOp: "clear",
                storeOp: "store",
              },
            ],
          })
          if (isParticleRibbon && ribbonCompositeBindGroup) {
            pass.setPipeline(ribbonCompositePipeline)
            pass.setBindGroup(0, ribbonCompositeBindGroup)
          } else {
            pass.setPipeline(pipeline)
            pass.setBindGroup(0, bindGroup)
          }
          pass.draw(3)
          pass.end()
          device!.queue.submit([encoder.finish()])
          animationFrame = requestAnimationFrame(frame)
        } catch (error) {
          fail(error)
        }
      }

      animationFrame = requestAnimationFrame(frame)
    }

    start().catch(fail)

    return () => {
      stopped = true
      cancelAnimationFrame(animationFrame)
      ribbonTarget?.destroy()
      device?.destroy()
      apiRef.current = null
    }
    // Mount once; `state`/`audioBands` are pushed in via apiRef through the
    // effects above rather than restarting the WebGPU session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fallback])

  if (fallback) {
    return (
      <span
        className={className}
        style={{
          display: "inline-block",
          width: size,
          height: size,
          borderRadius: "9999px",
          background: "currentColor",
          opacity: 0.5,
          animation: "liquid-orb-fallback-pulse 1.1s ease-in-out infinite",
          ...style,
        }}
      >
        <style>{`
          @keyframes liquid-orb-fallback-pulse {
            0%, 100% { opacity: 0.35; transform: scale(0.85); }
            50% { opacity: 0.8; transform: scale(1); }
          }
        `}</style>
      </span>
    )
  }

  return (
    <canvas
      ref={canvasRef}
      className={className}
      aria-hidden="true"
      style={{ width: size, height: size, display: "block", ...style }}
    />
  )
})

export default LiquidOrb
