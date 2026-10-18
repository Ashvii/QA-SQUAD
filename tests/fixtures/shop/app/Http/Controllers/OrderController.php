<?php

namespace App\Http\Controllers;

use App\Models\Order;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

class OrderController extends Controller
{
    /**
     * GET /api/orders
     */
    public function index(Request $request): JsonResponse
    {
        $orders = Order::where('user_id', $request->user()->id)->latest()->get();

        return response()->json($orders->map(fn (Order $order) => [
            'id' => $order->id,
            'product' => $order->product->name,
            'status' => $order->status,
        ]));
    }

    /**
     * GET /api/orders/{id}
     */
    public function show(int $id): JsonResponse
    {
        $order = Order::with('product')->findOrFail($id);

        return response()->json($order);
    }
}
