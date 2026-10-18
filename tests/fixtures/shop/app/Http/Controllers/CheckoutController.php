<?php

namespace App\Http\Controllers;

use App\Http\Requests\CheckoutRequest;
use App\Models\Order;
use App\Models\Product;
use Illuminate\Http\JsonResponse;

class CheckoutController extends Controller
{
    /**
     * POST /api/checkout  {"product_id": int, "quantity": int}
     */
    public function store(CheckoutRequest $request): JsonResponse
    {
        $product = Product::findOrFail($request->integer('product_id'));
        $quantity = $request->integer('quantity');

        if ($product->stock < $quantity) {
            return response()->json(['message' => 'Out of stock'], 422);
        }

        $product->stock = $product->stock - $quantity;
        $product->save();

        $order = Order::create([
            'user_id' => $request->user()->id,
            'product_id' => $product->id,
            'quantity' => $quantity,
            'total_cents' => $product->price_cents * $quantity,
            'status' => 'pending',
        ]);

        return response()->json($order, 201);
    }
}
