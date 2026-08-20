import type { TopProduct } from '../types'

export default function TopProducts({
  products,
  currency,
}: {
  products: TopProduct[]
  currency: string
}) {
  const max = Math.max(1, ...products.map((product) => product.quantity))

  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <h3 className="mb-3 font-bold text-(--color-ink)">الأكثر مبيعاً</h3>
      {products.length === 0 ? (
        <p className="text-sm text-(--color-ink-soft)">لا توجد مبيعات في هذه الفترة</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {products.map((product) => (
            <li key={product.title}>
              <div className="mb-1 flex items-center justify-between text-sm">
                <span className="font-medium text-(--color-ink)">{product.title}</span>
                <span className="ltr-num text-(--color-ink-soft)">
                  {product.quantity} قطعة · {product.revenue.toLocaleString()} {currency}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-(--color-bg)">
                <div
                  className="h-full rounded-full bg-(--color-accent-500)"
                  style={{ width: `${(product.quantity / max) * 100}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
