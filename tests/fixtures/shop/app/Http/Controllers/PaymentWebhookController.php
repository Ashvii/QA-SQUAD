<?php

namespace App\Http\Controllers;

use App\Models\Order;
use App\Models\Payment;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

class PaymentWebhookController extends Controller
{
    /**
     * POST /api/webhooks/payments  {"payment_id": str, "order_id": int, "amount_cents": int}
     */
    public function handle(Request $request): JsonResponse
    {
        $this->verifySignature($request);

        $payload = $request->json()->all();
        $order = Order::findOrFail($payload['order_id']);

        Payment::create([
            'order_id' => $order->id,
            'provider_ref' => $payload['payment_id'],
            'amount_cents' => $payload['amount_cents'],
        ]);

        $order->update(['status' => 'paid']);
        $order->user->increment('loyalty_points', intdiv($payload['amount_cents'], 100));

        return response()->json(['ok' => true]);
    }

    private function verifySignature(Request $request): void
    {
        $expected = hash_hmac('sha256', $request->getContent(), config('services.payments.webhook_secret'));

        abort_unless(hash_equals($expected, (string) $request->header('X-Signature')), 401);
    }
}
