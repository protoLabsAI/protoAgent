// ESM shim: re-export the UMD React global so bare `import ... from "react"` resolves
// to the SAME React instance the artifact's JSX (React.createElement) uses.
const R = window.React;
export default R;
export const {
  Children, Component, Fragment, Profiler, PureComponent, StrictMode, Suspense,
  cloneElement, createContext, createElement, createRef, forwardRef, isValidElement,
  lazy, memo, startTransition, useCallback, useContext, useDebugValue, useDeferredValue,
  useEffect, useId, useImperativeHandle, useInsertionEffect, useLayoutEffect, useMemo,
  useReducer, useRef, useState, useSyncExternalStore, useTransition, version,
} = R;
