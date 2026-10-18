<?php

namespace App\Models;

use App\Models\Concerns\BelongsToTenant;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Export extends Model
{
    use BelongsToTenant;

    protected $fillable = ['user_id', 'type', 'filters', 'status', 'path', 'row_count'];

    protected $casts = ['filters' => 'array', 'row_count' => 'integer'];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }
}
