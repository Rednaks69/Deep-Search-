"use client"

import { type CSSProperties, useEffect, useRef } from "react"
import {
  createShader,
  type ShaderHandle,
  type ShaderOptions,
  type ShaderTheme,
} from "./skullShaderEngine"

export type SkullShaderProps = {
  theme?: ShaderTheme
  background?: { dark?: string; light?: string }
  time?: number
  onError?: (error: Error) => void
  className?: string
  style?: CSSProperties
}

export function SkullShader({
  theme = "dark",
  background,
  time,
  onError,
  className,
  style,
}: SkullShaderProps) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const shader = useRef<ShaderHandle | null>(null)
  const latestTheme = useRef(theme)
  const latestTime = useRef(time)
  const latestOnError = useRef(onError)
  const dark = background?.dark ?? "#090909"
  const light = background?.light ?? "#ffffff"
  const animated = time === undefined

  useEffect(() => {
    latestTheme.current = theme
    shader.current?.setTheme(theme)
  }, [theme])

  useEffect(() => {
    latestTime.current = time
    if (time !== undefined) shader.current?.render(time)
  }, [time])

  useEffect(() => {
    latestOnError.current = onError
  }, [onError])

  useEffect(() => {
    const element = canvas.current
    if (!element) return
    let handle: ShaderHandle | null = null
    const controller = new AbortController()
    const options: ShaderOptions = {
      theme: latestTheme.current,
      background: { dark, light },
      autoplay: animated,
      signal: controller.signal,
      onError: (error) => {
        if (controller.signal.aborted) return
        if (latestOnError.current) latestOnError.current(error)
        else console.error(error)
      },
    }

    createShader(element, options)
      .then((created) => {
        if (controller.signal.aborted) {
          created.destroy()
          return
        }
        handle = created
        shader.current = created
        if (latestTheme.current !== options.theme)
          created.setTheme(latestTheme.current)
        if (latestTime.current !== undefined) created.render(latestTime.current)
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          options.onError?.(
            error instanceof Error ? error : new Error(String(error))
          )
      })
    return () => {
      controller.abort()
      handle?.destroy()
      shader.current = null
    }
  }, [dark, light, animated])

  return (
    <canvas
      ref={canvas}
      className={className}
      style={{ display: "block", width: "100%", height: "100%", ...style }}
      aria-hidden="true"
    />
  )
}
