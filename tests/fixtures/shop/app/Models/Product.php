<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Product extends Model
{
    protected $fillable = ['name', 'price_cents', 'stock'];

    protected $casts = ['price_cents' => 'integer', 'stock' => 'integer'];
}
